//! The original `_ActionProgressWindow`: a small modal window running a
//! shell command (through the sudo wrapper when asked) and showing what it
//! does, for downloads and installs.

use crate::ui::tr;
use adw::prelude::*;
use futures_util::StreamExt;
use gtk::glib;
use linexin_plugin_api::Host;
use regex::Regex;
use std::cell::{Cell, RefCell};
use std::io::Read;
use std::process::{Command, Stdio};
use std::rc::Rc;
use std::sync::LazyLock;
use std::time::Duration;

/// What to do once the window closes, with whether the command succeeded.
pub type OnClose = Box<dyn FnOnce(bool)>;

struct State {
    window: adw::Window,
    status: gtk::Label,
    progress: gtk::ProgressBar,
    is_ollama: bool,
    model_name: String,
    success: Cell<bool>,
    finished: Cell<bool>,
    real_progress: Cell<bool>,
    on_close: RefCell<Option<OnClose>>,
}

enum Event {
    Line(String),
    Finished(i32),
    Failed,
}

/// Open the window and start `command` (under `bash -c`, through the sudo
/// wrapper when `sudo` is given). `is_ollama` switches to the model-download
/// wording and to percentages as progress.
#[allow(clippy::too_many_arguments)]
pub fn launch(
    parent: Option<&gtk::Window>,
    title: &str,
    command: &str,
    is_ollama: bool,
    initial_status: Option<&str>,
    on_close: Option<OnClose>,
    sudo: Option<Host>,
    model_name: Option<&str>,
) -> adw::Window {
    let window = glib::Object::builder::<adw::Window>()
        .property("title", title)
        .property("transient-for", parent)
        .property("modal", true)
        .build();
    window.set_default_size(500, 200);
    window.set_deletable(false);

    let content_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
    window.set_content(Some(&content_box));
    content_box.append(&adw::HeaderBar::new());
    let content = gtk::Box::new(gtk::Orientation::Vertical, 16);
    content.set_margin_top(24);
    content.set_margin_bottom(24);
    content.set_margin_start(24);
    content.set_margin_end(24);
    content_box.append(&content);

    let initial = initial_status
        .filter(|status| !status.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| tr("Starting operation..."));
    let status = gtk::Label::new(Some(&initial));
    status.add_css_class("title-4");
    status.set_wrap(true);
    content.append(&status);
    let progress = gtk::ProgressBar::new();
    progress.set_margin_top(12);
    content.append(&progress);

    let state = Rc::new(State {
        window: window.clone(),
        status,
        progress,
        is_ollama,
        model_name: model_name.unwrap_or_default().to_owned(),
        success: Cell::new(false),
        finished: Cell::new(false),
        real_progress: Cell::new(false),
        on_close: RefCell::new(on_close),
    });
    if !is_ollama {
        state.progress.set_pulse_step(0.1);
        let weak = Rc::downgrade(&state);
        glib::timeout_add_local(Duration::from_millis(100), move || match weak.upgrade() {
            Some(state) if !state.finished.get() && !state.real_progress.get() => {
                state.progress.pulse();
                glib::ControlFlow::Continue
            }
            _ => glib::ControlFlow::Break,
        });
    }
    {
        let keep = state.clone();
        window.connect_close_request(move |_| {
            if !keep.finished.get() {
                return glib::Propagation::Stop;
            }
            if let Some(callback) = keep.on_close.borrow_mut().take() {
                callback(keep.success.get());
            }
            glib::Propagation::Proceed
        });
    }

    let (sender, mut events) = futures_channel::mpsc::unbounded::<Event>();
    let command = command.to_owned();
    std::thread::spawn(move || run_process(&command, sudo, &sender));
    let weak = Rc::downgrade(&state);
    glib::spawn_future_local(async move {
        while let Some(event) = events.next().await {
            let Some(state) = weak.upgrade() else {
                break;
            };
            match event {
                Event::Line(line) => state.parse_and_append(&line),
                Event::Finished(code) => {
                    state.finished.set(true);
                    state.on_finish(code);
                }
                Event::Failed => {
                    state.finished.set(true);
                    state.status.set_label(&tr("Process failed to start."));
                }
            }
        }
    });
    window
}

/// The original `run_process`: lines split at `\n` and `\r`.
fn run_process(
    command: &str,
    sudo: Option<Host>,
    events: &futures_channel::mpsc::UnboundedSender<Event>,
) {
    let mut argv = vec!["bash".to_owned(), "-c".to_owned(), command.to_owned()];
    if let Some(host) = sudo {
        host.sudo_start_session();
        argv.insert(0, host.sudo_wrapper_path());
    }
    // The original Center locked its window while the wrapper ran.
    if let Some(host) = sudo {
        host.lock_ui();
    }
    let spawned = (|| -> std::io::Result<_> {
        let (reader, writer) = std::io::pipe()?;
        let error_writer = writer.try_clone()?;
        let child = Command::new(&argv[0])
            .args(&argv[1..])
            .stdout(writer)
            .stderr(error_writer)
            .stdin(Stdio::inherit())
            .spawn()?;
        Ok((reader, child))
    })();
    let (mut reader, mut child) = match spawned {
        Ok(spawned) => spawned,
        Err(error) => {
            if let Some(host) = sudo {
                host.unlock_ui();
                host.sudo_stop_session();
            }
            println!("Error launching process: {error}");
            let _ = events.unbounded_send(Event::Failed);
            return;
        }
    };
    let mut buffer = [0u8; 4096];
    let mut line: Vec<u8> = Vec::new();
    loop {
        match reader.read(&mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(read) => {
                for &byte in &buffer[..read] {
                    if byte == b'\n' || byte == b'\r' {
                        if !line.is_empty() {
                            let _ = events.unbounded_send(Event::Line(
                                String::from_utf8_lossy(&line).into_owned(),
                            ));
                            line.clear();
                        }
                    } else {
                        line.push(byte);
                    }
                }
            }
        }
    }
    if !line.is_empty() {
        let _ = events.unbounded_send(Event::Line(String::from_utf8_lossy(&line).into_owned()));
    }
    let code = child
        .wait()
        .map(|status| {
            use std::os::unix::process::ExitStatusExt;
            status
                .code()
                .unwrap_or_else(|| -status.signal().unwrap_or(0))
        })
        .unwrap_or(1);
    if let Some(host) = sudo {
        host.unlock_ui();
        host.sudo_stop_session();
    }
    let _ = events.unbounded_send(Event::Finished(code));
}

impl State {
    /// The original `parse_and_append`: the useful part of a line becomes
    /// the status, percentages the progress.
    fn parse_and_append(&self, line: &str) {
        println!("{line}");
        static ANSI: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])").expect("valid"));
        static BORDERS: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[+\-|*=\s]+$").expect("valid"));
        static BAR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[#\s.]+$").expect("valid"));
        static CURL: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[#\s]+(\d+(?:\.\d+)?)%\s*$").expect("valid"));
        static SPINNER: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[\\|/\-⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏\s]+$").expect("valid"));
        static NPM: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"^[⸩⸨()\[\]#=>.\-\s⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]+$").expect("valid"));
        static INTEGER_PERCENT: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"(\d+)%").expect("valid"));
        static PERCENT: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"(\d+(?:\.\d+)?)%").expect("valid"));

        let clean_line = ANSI.replace_all(line, "");
        let clean_line = crate::conversations::python_strip(&clean_line);
        let filtered = BORDERS.replace(clean_line, "");
        let filtered = filtered.trim_matches(|c| c == '|' || c == ' ' || c == '\t');
        if filtered.is_empty() || BAR.is_match(filtered) {
            return;
        }
        if let Some(captures) = CURL.captures(filtered) {
            let percent: f64 = captures[1].parse().unwrap_or(0.0);
            self.real_progress.set(true);
            self.progress.set_fraction(percent / 100.0);
            self.status.set_label(&format!("{percent:.1}%"));
            return;
        }
        if SPINNER.is_match(filtered) || NPM.is_match(filtered) {
            return;
        }
        if self.is_ollama {
            let display_name = if self.model_name.is_empty() {
                tr("model")
            } else {
                self.model_name.clone()
            };
            let lower = filtered.to_lowercase();
            let label = if lower.contains("pulling manifest") {
                Some(tr("Fetching model info for {}...").replacen("{}", &display_name, 1))
            } else if lower.contains("pulling") {
                Some(tr("Downloading {}...").replacen("{}", &display_name, 1))
            } else if lower.contains("verifying") {
                Some(tr("Verifying download integrity..."))
            } else if lower.contains("writing") {
                Some(tr("Finalizing {}...").replacen("{}", &display_name, 1))
            } else if lower.contains("success") {
                Some(tr("Successfully downloaded {}!").replacen("{}", &display_name, 1))
            } else {
                None
            };
            if let Some(label) = label {
                self.status.set_label(&label);
            }
            if let Some(captures) = INTEGER_PERCENT.captures(clean_line) {
                let value: f64 = captures[1].parse().unwrap_or(0.0);
                self.progress.set_fraction(value / 100.0);
            }
        } else {
            if let Some(captures) = PERCENT.captures(clean_line) {
                self.real_progress.set(true);
                let value: f64 = captures[1].parse().unwrap_or(0.0);
                self.progress.set_fraction(value / 100.0);
            }
            let chars: Vec<char> = filtered.chars().collect();
            let truncated = if chars.len() > 60 {
                format!("{}...", chars[..60].iter().collect::<String>())
            } else {
                filtered.to_owned()
            };
            self.status.set_label(&truncated);
        }
    }

    /// The original `on_finish`.
    fn on_finish(self: &Rc<Self>, code: i32) {
        if self.success.get() {
            return;
        }
        if code == 0 {
            self.status
                .set_label(&tr("Operation completed successfully."));
            self.progress.set_fraction(1.0);
            self.success.set(true);
            self.window.set_deletable(true);
            let window = self.window.downgrade();
            glib::timeout_add_local_once(Duration::from_millis(1500), move || {
                if let Some(window) = window.upgrade() {
                    window.close();
                }
            });
        } else {
            self.window.set_deletable(true);
            self.status.set_label(
                &tr("Operation failed with exit code {}. Check console output.").replacen(
                    "{}",
                    &code.to_string(),
                    1,
                ),
            );
            self.success.set(false);
        }
    }
}
