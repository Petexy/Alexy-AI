//! Screen Awareness: the original's `_capture_screenshot` (the XDG desktop
//! portal, then command-line tools), `_process_screenshot` and
//! `_cleanup_screenshot_tmp`.

use base64::Engine;
use gtk::gio;
use gtk::glib::{self, prelude::*};
use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::rc::Rc;
use std::time::{Duration, Instant};

const SCREENSHOT_DIR: &str = "/tmp/linexin";

/// `tempfile.gettempdir()`.
pub fn temp_dir() -> PathBuf {
    ["TMPDIR", "TEMP", "TMP"]
        .iter()
        .filter_map(std::env::var_os)
        .map(PathBuf::from)
        .find(|dir| dir.is_dir())
        .unwrap_or_else(|| PathBuf::from("/tmp"))
}

/// `str(GLib.Error)` as PyGObject shows it.
fn python_gerror(error: &glib::Error) -> String {
    use glib::translate::ToGlibPtr;
    let raw: *const glib::ffi::GError = error.to_glib_none().0;
    // SAFETY: a live GError owned by `error`.
    let code = unsafe { (*raw).code };
    format!("{}: {} ({code})", error.domain().as_str(), error.message())
}

/// The original `_capture_screenshot`: `(mime type, base64)`, or `None`.
pub fn capture_screenshot() -> Option<(String, String)> {
    {
        use std::os::unix::fs::DirBuilderExt;
        let _ = std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(SCREENSHOT_DIR);
    }
    let id = glib::uuid_string_random().replace('-', "");
    let path = Path::new(SCREENSHOT_DIR).join(format!("screen_{id}.png"));
    println!(
        "[Screen Awareness] Attempting screenshot -> {}",
        path.display()
    );
    println!("[Screen Awareness] Trying XDG Desktop Portal (org.freedesktop.portal.Screenshot)...");
    let mut captured = match capture_via_portal(&path) {
        Ok(true) => {
            println!("[Screen Awareness] Screenshot captured via XDG Desktop Portal");
            true
        }
        Ok(false) => {
            println!("[Screen Awareness] XDG Desktop Portal returned no image");
            false
        }
        Err(error) => {
            println!("[Screen Awareness] Portal screenshot failed: {error}");
            false
        }
    };
    if !captured {
        println!("[Screen Awareness] Falling back to CLI tools...");
        captured = capture_via_cli(&path);
    }
    if !captured || !path.is_file() {
        println!("[Screen Awareness] All screenshot methods failed");
        return None;
    }
    let size = std::fs::metadata(&path)
        .map(|metadata| metadata.len())
        .unwrap_or(0);
    println!("[Screen Awareness] Processing screenshot ({size} bytes)");
    match std::fs::read(&path) {
        Ok(raw) => Some((
            "image/png".into(),
            base64::engine::general_purpose::STANDARD.encode(raw),
        )),
        Err(error) => {
            println!("[Screen Awareness] Failed to read screenshot: {error}");
            None
        }
    }
}

/// The original `_capture_via_portal`: the portal's non-interactive
/// screenshot, waited for (the main loop keeps running meanwhile).
fn capture_via_portal(destination: &Path) -> Result<bool, String> {
    let bus = gio::bus_get_sync(gio::BusType::Session, None::<&gio::Cancellable>)
        .map_err(|error| python_gerror(&error))?;
    let token = format!(
        "linexin_{}",
        &glib::uuid_string_random().replace('-', "")[..8]
    );
    let sender = bus
        .unique_name()
        .map(|name| name.replace('.', "_").trim_start_matches(':').to_owned())
        .unwrap_or_default();
    let handle = format!("/org/freedesktop/portal/desktop/request/{sender}/{token}");
    let uri: Rc<RefCell<Option<String>>> = Rc::default();
    let responded = Rc::new(std::cell::Cell::new(false));
    #[allow(deprecated)]
    let subscription = {
        let (uri, responded) = (uri.clone(), responded.clone());
        bus.signal_subscribe(
            Some("org.freedesktop.portal.Desktop"),
            Some("org.freedesktop.portal.Request"),
            Some("Response"),
            Some(&handle),
            None,
            gio::DBusSignalFlags::NO_MATCH_RULE,
            move |_, _, _, _, _, parameters| {
                let response = parameters.child_value(0).get::<u32>().unwrap_or(1);
                if response == 0 {
                    let results = glib::VariantDict::new(Some(&parameters.child_value(1)));
                    if let Some(found) = results
                        .lookup_value("uri", None)
                        .and_then(|value| value.get::<String>())
                        .filter(|found| !found.is_empty())
                    {
                        uri.replace(Some(found));
                    }
                }
                responded.set(true);
            },
        )
    };
    let options = glib::VariantDict::new(None);
    options.insert_value("handle_token", &token.to_variant());
    options.insert_value("interactive", &false.to_variant());
    let call = bus.call_sync(
        Some("org.freedesktop.portal.Desktop"),
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Screenshot",
        "Screenshot",
        Some(&("", options.end()).to_variant()),
        None,
        gio::DBusCallFlags::NONE,
        5000,
        None::<&gio::Cancellable>,
    );
    let result = call.map(|_| {
        let context = glib::MainContext::default();
        let deadline = Instant::now() + Duration::from_secs(10);
        while !responded.get() && Instant::now() < deadline {
            context.iteration(false);
            if !responded.get() {
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    });
    #[allow(deprecated)]
    bus.signal_unsubscribe(subscription);
    result.map_err(|error| python_gerror(&error))?;

    let Some(uri) = uri.take() else {
        return Ok(false);
    };
    let source = uri.strip_prefix("file://").unwrap_or(&uri).to_owned();
    if let Err(error) = std::fs::copy(&source, destination) {
        println!("[Screen Awareness] Failed to copy portal screenshot: {error}");
        return Ok(false);
    }
    if !destination.is_file() {
        return Ok(false);
    }
    match std::fs::remove_file(&source) {
        Ok(()) => println!("[Screen Awareness] Removed portal source file {source}"),
        Err(error) => println!("[Screen Awareness] Could not remove portal source file: {error}"),
    }
    Ok(true)
}

/// `shutil.which`.
fn which(name: &str) -> bool {
    std::env::var_os("PATH").is_some_and(|path| {
        std::env::split_paths(&path).any(|dir| {
            use std::os::unix::fs::PermissionsExt;
            std::fs::metadata(dir.join(name)).is_ok_and(|metadata| {
                metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
            })
        })
    })
}

/// The original `_capture_via_cli`.
fn capture_via_cli(path: &Path) -> bool {
    let path = path.to_string_lossy().into_owned();
    let commands: [Vec<&str>; 5] = [
        vec!["grim", &path],
        vec!["gnome-screenshot", "-f", &path],
        vec!["spectacle", "-b", "-n", "-f", "-o", &path],
        vec!["scrot", &path],
        vec!["import", "-window", "root", &path],
    ];
    for command in commands {
        if !which(command[0]) {
            println!("[Screen Awareness]   {}: not found, skipping", command[0]);
            continue;
        }
        println!("[Screen Awareness]   Trying {}...", command[0]);
        let Ok(mut child) = Command::new(command[0])
            .args(&command[1..])
            .stdin(Stdio::inherit())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
        else {
            continue;
        };
        let start = Instant::now();
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break Some(status),
                Ok(None) if start.elapsed() < Duration::from_secs(10) => {
                    std::thread::sleep(Duration::from_millis(20));
                }
                _ => {
                    let _ = child.kill();
                    let _ = child.wait();
                    break None;
                }
            }
        };
        let Some(status) = status else {
            continue;
        };
        if status.success() && Path::new(&path).is_file() {
            println!(
                "[Screen Awareness]   Screenshot captured via {}",
                command[0]
            );
            return true;
        }
        let code = {
            use std::os::unix::process::ExitStatusExt;
            status
                .code()
                .unwrap_or_else(|| -status.signal().unwrap_or(0))
        };
        println!("[Screen Awareness]   {} failed (rc={code})", command[0]);
    }
    false
}

/// The original `_cleanup_screenshot_tmp`.
pub fn cleanup_tmp() {
    let dir = Path::new(SCREENSHOT_DIR);
    if dir.is_dir() {
        match std::fs::remove_dir_all(dir) {
            Ok(()) => println!("[Screen Awareness] Cleaned up {SCREENSHOT_DIR}"),
            Err(error) => {
                println!("[Screen Awareness] Failed to clean up {SCREENSHOT_DIR}: {error}")
            }
        }
    }
}
