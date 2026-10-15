//! Development harness: loads the built widget library through the C ABI with a
//! host whose translation services are the Center's own (dictionaries, `{}`
//! patterns and `translate_dialog` from linexin-localization), shows it in an
//! `AdwApplicationWindow` (or opens its `--compact` voice window) and plays the
//! steps given on the command line, as `examples/python_reference.py` does
//! with the original widget:
//!
//! ```text
//! type=TEXT          the chat entry's text
//! send               click the send (or stop) button
//! wait-idle          wait until a reply has ended (input usable again)
//! wait-dialog        wait until a message dialog is open
//! wait-window        wait until a progress window is open
//! wait-window-done   wait until the progress window can be closed (or closed)
//! answer=RESPONSE    answer the open dialog (RESPONSE=PASSWORD types one)
//! new                click the new conversation button
//! conversations      toggle the conversations button
//! conv-row=TITLE     open the saved conversation titled TITLE
//! conv-edit=TITLE    click its rename button
//! conv-delete=TITLE  click its delete button
//! rename=TEXT        type TEXT into the rename row and press Enter
//! filter=LABEL       pick the backend filter button labelled LABEL
//! settings           click the settings button
//! settings-page=NAME show the settings page NAME (assistant, agents, speech, theme)
//! close-settings     click the settings sidebar's close button
//! switch=TITLE       flip the switch row titled TITLE
//! combo=TITLE=INDEX  select item INDEX of the combo row titled TITLE
//! entryrow=TITLE=TEXT the text of the entry row titled TITLE
//! click=LABEL        click the (first mapped) button labelled LABEL
//! screen             toggle the screen awareness button
//! mic                toggle the microphone button
//! expand             expand every reasoning section in the chat
//! settle             wait 1.5 s
//! sleep=SECONDS      wait
//! print              print what the widget shows
//! shot=PATH          render the window
//! dialog-shot=PATH   render the open dialog
//! window-shot=PATH   render the open progress window
//! files              print the config file and the saved conversations
//! ```
//!
//! Run it inside a headless compositor, with the fake commands of
//! examples/compare.sh first on `PATH`. The host accepts only the password in
//! `$FAKE_PASSWORD`, and its sudo wrapper is `$FAKE_WRAPPER`.

use std::cell::RefCell;
use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use adw::prelude::*;
use gtk::glib::translate::{FromGlibPtrBorrow, FromGlibPtrFull, ToGlibPtr};
use gtk::{gio, glib, graphene};
use linexin_localization::{Localization, widgets};
use linexin_plugin_api::{
    ABI_VERSION, CompactVoiceContextV1, CreateContextV1, ENTRY_SYMBOL, EntryV1, Header, HostApiV1,
    WidgetDescriptorV1,
};

const MANIFEST_DIR: &str = env!("CARGO_MANIFEST_DIR");
const SETTLE: Duration = Duration::from_millis(1500);
const PATIENCE: Duration = Duration::from_secs(40);

struct Options {
    library: PathBuf,
    localization: PathBuf,
    /// The Center's stylesheet; its own copy (`CENTER_STYLE_CSS`) without one.
    css: Option<PathBuf>,
    language: CString,
    dark: bool,
    width: i32,
    height: i32,
    compact_mode: bool,
    voice: bool,
    conversation: Option<CString>,
    compact_window: bool,
    steps: Vec<String>,
}

static OPTIONS: OnceLock<Options> = OnceLock::new();

fn options() -> &'static Options {
    OPTIONS
        .get()
        .expect("options are set before the host is used")
}

fn parse_options() -> Result<Options, String> {
    let manifest = Path::new(MANIFEST_DIR);
    let mut options = Options {
        library: beside_the_examples("libalexy_ai.so"),
        localization: manifest.join("data/usr/share/linexin/widgets/localization"),
        css: None,
        language: c"en_US".into(),
        dark: false,
        width: 1300,
        height: 820,
        compact_mode: false,
        voice: false,
        conversation: None,
        compact_window: false,
        steps: Vec::new(),
    };
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        let mut value = || args.next().ok_or(format!("{arg} needs a value"));
        match arg.as_str() {
            "--library" => options.library = value()?.into(),
            "--localization" => options.localization = value()?.into(),
            "--css" => options.css = Some(value()?.into()),
            "--lang" => options.language = CString::new(value()?).map_err(|e| e.to_string())?,
            "--dark" => options.dark = true,
            "--width" => options.width = value()?.parse().map_err(|_| "bad --width")?,
            "--height" => options.height = value()?.parse().map_err(|_| "bad --height")?,
            "--compact-mode" => options.compact_mode = true,
            "--voice" => options.voice = true,
            "--conversation" => {
                options.conversation = Some(CString::new(value()?).map_err(|e| e.to_string())?)
            }
            "--compact-window" => options.compact_window = true,
            "--widget" => {
                value()?;
            }
            step if !step.starts_with("--") => options.steps.push(step.to_owned()),
            _ => return Err(format!("unknown argument {arg}")),
        }
    }
    Ok(options)
}

fn evaluate_dictionary(path: &Path) -> Result<Vec<(String, String)>, String> {
    let output = std::process::Command::new("python3")
        .args([
            "-c",
            "import json, runpy, sys\n\
             t = runpy.run_path(sys.argv[1])['translations']\n\
             print(json.dumps([[k, v] for k, v in t.items()]))",
        ])
        .arg(path)
        .output()
        .map_err(|e| e.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).into_owned());
    }
    serde_json::from_slice(&output.stdout).map_err(|e| e.to_string())
}

fn install_localization() {
    let options = options();
    // The dictionaries are not plain literals (`translations.update(...)`):
    // Python evaluates them, as the Center's bridge does.
    let localization = Localization::load(
        std::slice::from_ref(&options.localization),
        &options.language.to_string_lossy(),
        Some(&evaluate_dictionary),
    );
    for diagnostic in localization.diagnostics() {
        eprintln!("localization: {diagnostic}");
    }
    linexin_localization::install(localization);
}

/// A line in `$FAKE_LOG` (or `$FAKE_LOCK_LOG`), beside the fake commands'.
fn log(variable: &str, line: &str) {
    if let Some(path) = std::env::var_os(variable)
        && let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
    {
        let _ = writeln!(file, "{line}");
    }
}

unsafe extern "C" fn language() -> *const c_char {
    options().language.as_ptr()
}

unsafe extern "C" fn translate(domain: *const c_char, text: *const c_char) -> *mut c_char {
    if domain.is_null() || text.is_null() {
        return std::ptr::null_mut();
    }
    let (domain, text) = unsafe { (CStr::from_ptr(domain), CStr::from_ptr(text)) };
    let translated = linexin_localization::global()
        .translate(&text.to_string_lossy(), &domain.to_string_lossy());
    CString::new(translated).map_or(std::ptr::null_mut(), CString::into_raw)
}

unsafe extern "C" fn free_string(string: *mut c_char) {
    if !string.is_null() {
        drop(unsafe { CString::from_raw(string) });
    }
}

unsafe extern "C" fn translate_widget(domain: *const c_char, widget: *mut c_void) {
    if domain.is_null() || widget.is_null() {
        return;
    }
    let domain = unsafe { CStr::from_ptr(domain) }.to_string_lossy();
    let widget = unsafe { gtk::Widget::from_glib_borrow(widget.cast::<gtk::ffi::GtkWidget>()) };
    widgets::translate_dialog(&widget, &domain);
}

/// The host's remembered password (the Center's `sudo_manager.user_password`).
static PASSWORD: Mutex<Option<String>> = Mutex::new(None);

unsafe extern "C" fn no_op() {}
unsafe extern "C" fn lock_ui() {
    log("FAKE_LOCK_LOG", "lock");
}
unsafe extern "C" fn unlock_ui() {
    log("FAKE_LOCK_LOG", "unlock");
}
unsafe extern "C" fn validate_password(password: *const c_char) -> bool {
    log("FAKE_LOG", "sudo validate_password");
    let expected = std::env::var("FAKE_PASSWORD").unwrap_or_default();
    !password.is_null()
        && !expected.is_empty()
        && unsafe { CStr::from_ptr(password) }.to_bytes() == expected.as_bytes()
}
unsafe extern "C" fn set_password(password: *const c_char) {
    log("FAKE_LOG", "sudo set_password");
    let password = (!password.is_null()).then(|| {
        unsafe { CStr::from_ptr(password) }
            .to_string_lossy()
            .into_owned()
    });
    *PASSWORD.lock().unwrap() = password;
}
unsafe extern "C" fn has_password() -> bool {
    PASSWORD
        .lock()
        .unwrap()
        .as_ref()
        .is_some_and(|p| !p.is_empty())
}
unsafe extern "C" fn forget_password() {
    log("FAKE_LOG", "sudo forget_password");
    *PASSWORD.lock().unwrap() = None;
}
unsafe extern "C" fn wrapper_path() -> *const c_char {
    static WRAPPER: OnceLock<CString> = OnceLock::new();
    WRAPPER
        .get_or_init(|| {
            CString::new(std::env::var("FAKE_WRAPPER").unwrap_or_else(|_| "/bin/false".into()))
                .unwrap_or_default()
        })
        .as_ptr()
}
unsafe extern "C" fn start_session() {
    log("FAKE_LOG", "sudo start_privileged_session");
}
unsafe extern "C" fn stop_session() {
    log("FAKE_LOG", "sudo stop_privileged_session");
}
unsafe extern "C" fn run_privileged(argv: *const *const c_char) -> c_int {
    let mut args = Vec::new();
    for index in 0.. {
        let arg = unsafe { *argv.add(index) };
        if arg.is_null() {
            break;
        }
        args.push(
            unsafe { CStr::from_ptr(arg) }
                .to_string_lossy()
                .into_owned(),
        );
    }
    log(
        "FAKE_LOG",
        &format!("sudo run_privileged {}", args.join(" ")),
    );
    0
}
unsafe extern "C" fn askpass_path() -> *const c_char {
    c"/bin/false".as_ptr()
}
unsafe extern "C" fn compact_mode(_: *mut c_void) -> bool {
    options().compact_mode
}

static HOST: HostApiV1 = HostApiV1 {
    abi_version: ABI_VERSION,
    struct_size: size_of::<HostApiV1>() as u32,
    language,
    translate,
    free_string,
    translate_widget,
    lock_ui,
    unlock_ui,
    sudo_validate_password: validate_password,
    sudo_set_password: set_password,
    sudo_has_password: has_password,
    sudo_forget_password: forget_password,
    sudo_wrapper_path: wrapper_path,
    sudo_start_session: start_session,
    sudo_stop_session: stop_session,
    sudo_run_privileged: run_privileged,
    refresh_widgets: no_op,
    sudo_askpass_path: askpass_path,
    window_compact_mode: compact_mode,
};

fn load_descriptor(path: &Path) -> Result<&'static WidgetDescriptorV1, String> {
    // SAFETY: a trusted development build of the widget.
    let library = unsafe { libloading::Library::new(path) }.map_err(|e| e.to_string())?;
    // Like the Center, never unload: GTK keeps pointers into the library.
    let library = Box::leak(Box::new(library));
    let entry = unsafe { library.get::<EntryV1>(ENTRY_SYMBOL) }.map_err(|e| e.to_string())?;
    let descriptor = unsafe { entry(&HOST) };
    if !unsafe { Header::is_compatible(descriptor.cast(), size_of::<WidgetDescriptorV1>()) } {
        return Err("incompatible descriptor".into());
    }
    Ok(unsafe { &*descriptor })
}

fn find_all<T: IsA<gtk::Widget>>(widget: &gtk::Widget, found: &mut Vec<T>) {
    if let Ok(widget) = widget.clone().downcast::<T>() {
        found.push(widget);
    }
    let mut child = widget.first_child();
    while let Some(current) = child {
        find_all(&current, found);
        child = current.next_sibling();
    }
}

fn all<T: IsA<gtk::Widget>>(widget: &impl IsA<gtk::Widget>) -> Vec<T> {
    let mut found = Vec::new();
    find_all(widget.as_ref(), &mut found);
    found
}

fn children(widget: &impl IsA<gtk::Widget>) -> Vec<gtk::Widget> {
    let mut found = Vec::new();
    let mut child = widget.as_ref().first_child();
    while let Some(current) = child {
        child = current.next_sibling();
        found.push(current);
    }
    found
}

fn render(widget: &impl IsA<gtk::Widget>, path: &Path) -> Result<(), String> {
    let widget = widget.as_ref();
    let (width, height) = (widget.width(), widget.height());
    let snapshot = gtk::Snapshot::new();
    gtk::WidgetPaintable::new(Some(widget)).snapshot(&snapshot, width.into(), height.into());
    let node = snapshot.to_node().ok_or("nothing was drawn")?;
    let renderer = widget
        .native()
        .and_then(|native| native.renderer())
        .ok_or("no renderer")?;
    let viewport = graphene::Rect::new(0.0, 0.0, width as f32, height as f32);
    renderer
        .render_texture(&node, Some(&viewport))
        .save_to_png(path)
        .map_err(|e| e.to_string())?;
    println!("rendered {width}x{height} to {}", path.display());
    Ok(())
}

async fn sleep(duration: Duration) {
    glib::timeout_future(duration).await;
}

/// Wait (up to [`PATIENCE`]) until `ready` holds.
async fn until(what: &str, ready: impl Fn() -> bool) -> Result<(), String> {
    let start = Instant::now();
    while !ready() {
        if start.elapsed() > PATIENCE {
            return Err(format!("timed out waiting for {what}"));
        }
        sleep(Duration::from_millis(50)).await;
    }
    Ok(())
}

#[allow(deprecated)] // Adw.MessageDialog, as the Python widget uses
fn message_dialogs() -> Vec<adw::MessageDialog> {
    gtk::Window::list_toplevels()
        .into_iter()
        .filter_map(|window| window.downcast::<adw::MessageDialog>().ok())
        .filter(|dialog| dialog.get_visible())
        .collect()
}

/// The open `_ActionProgressWindow`s: plain `Adw.Window`s with a transient parent.
#[allow(deprecated)]
fn progress_windows() -> Vec<adw::Window> {
    gtk::Window::list_toplevels()
        .into_iter()
        .filter(|window| !window.is::<adw::MessageDialog>())
        .filter_map(|window| window.downcast::<adw::Window>().ok())
        .filter(|window| window.get_visible() && window.transient_for().is_some())
        .collect()
}

fn q(text: &str) -> String {
    format!("{text:?}")
}

fn label_text(label: &gtk::Label) -> String {
    label.label().to_string()
}

#[allow(deprecated)]
fn print_dialogs() {
    for dialog in message_dialogs() {
        println!(
            "dialog heading={:?} body={:?}",
            dialog.heading().unwrap_or_default(),
            dialog.body()
        );
        let responses: Vec<String> = [
            "cancel",
            "authenticate",
            "deny",
            "allow",
            "ok",
            "create",
            "rename",
        ]
        .iter()
        .filter(|id| dialog.has_response(id))
        .map(|id| format!("{id}={}", dialog.response_label(id)))
        .collect();
        println!("  responses {responses:?}");
    }
    for window in progress_windows() {
        let labels: Vec<String> = all::<gtk::Label>(&window)
            .iter()
            .filter(|label| label.get_visible() && !label.label().is_empty())
            .map(label_text)
            .collect();
        let fractions: Vec<String> = all::<gtk::ProgressBar>(&window)
            .iter()
            .map(|bar| format!("{:.2}", bar.fraction()))
            .collect();
        println!(
            "progress title={:?} deletable={} labels={labels:?} fraction={fractions:?}",
            window.title().unwrap_or_default(),
            window.is_deletable()
        );
    }
}

/// The chat entry's text view and the input row's buttons.
struct Input {
    view: gtk::TextView,
    send: gtk::Button,
    mic: gtk::ToggleButton,
    screen: gtk::ToggleButton,
    spinner: gtk::Spinner,
}

fn input(widget: &gtk::Widget) -> Option<Input> {
    let scrolled = all::<gtk::ScrolledWindow>(widget)
        .into_iter()
        .find(|scrolled| scrolled.has_css_class("multiline-entry"))?;
    let view = all::<gtk::TextView>(&scrolled).into_iter().next()?;
    let send = scrolled.next_sibling()?.downcast::<gtk::Button>().ok()?;
    let mic = send.next_sibling()?.downcast::<gtk::ToggleButton>().ok()?;
    let screen = mic.next_sibling()?.downcast::<gtk::ToggleButton>().ok()?;
    let spinner = screen.next_sibling()?.downcast::<gtk::Spinner>().ok()?;
    Some(Input {
        view,
        send,
        mic,
        screen,
        spinner,
    })
}

/// The header's buttons: new conversation, conversations, settings.
fn header(widget: &gtk::Widget) -> Option<(gtk::Button, gtk::ToggleButton, gtk::Button)> {
    let toggle = all::<gtk::ToggleButton>(widget)
        .into_iter()
        .find(|toggle| toggle.has_css_class("circular"))?;
    let new = toggle.prev_sibling()?.downcast::<gtk::Button>().ok()?;
    let settings = toggle.next_sibling()?.downcast::<gtk::Button>().ok()?;
    Some((new, toggle, settings))
}

fn button_state(button: &impl IsA<gtk::Widget>) -> String {
    let button = button.as_ref();
    let mut text = String::from(if button.is_sensitive() { "on" } else { "off" });
    if let Some(toggle) = button.downcast_ref::<gtk::ToggleButton>() {
        text.push_str(if toggle.is_active() { ",active" } else { "" });
    }
    if button.has_css_class("suggested-action") {
        text.push_str(",suggested");
    }
    if let Some(tooltip) = button.tooltip_text() {
        text.push_str(&format!(",tip={tooltip:?}"));
    }
    text
}

fn image_state(image: &gtk::Image) -> String {
    match image.storage_type() {
        gtk::ImageType::IconName => format!("icon:{}", image.icon_name().unwrap_or_default()),
        gtk::ImageType::Paintable => "file".into(),
        gtk::ImageType::Gicon => "gicon".into(),
        _ => "empty".into(),
    }
}

fn classes(widget: &impl IsA<gtk::Widget>) -> String {
    widget
        .as_ref()
        .css_classes()
        .iter()
        .map(|class| class.to_string())
        .collect::<Vec<_>>()
        .join(",")
}

fn describe_rows(list: &gtk::ListBox, lines: &mut Vec<String>) {
    for row in children(list) {
        if let Ok(action) = row.clone().downcast::<adw::ActionRow>() {
            let prefix = all::<gtk::Image>(&action)
                .iter()
                .filter(|image| {
                    image.get_visible() && image.storage_type() == gtk::ImageType::IconName
                })
                .map(|image| image.icon_name().unwrap_or_default().to_string())
                .collect::<Vec<_>>();
            lines.push(format!(
                "  conv {:?} {:?} icons={prefix:?}",
                action.title(),
                action.subtitle().unwrap_or_default()
            ));
        } else if let Ok(entry) = row.clone().downcast::<adw::EntryRow>() {
            lines.push(format!(
                "  rename {:?} text={:?}",
                entry.title(),
                entry.text()
            ));
        } else {
            let labels: Vec<String> = all::<gtk::Label>(&row).iter().map(label_text).collect();
            lines.push(format!("  header {labels:?}"));
        }
    }
}

fn describe_preferences(page: &gtk::Widget, lines: &mut Vec<String>) {
    for group in all::<adw::PreferencesGroup>(page) {
        if !group.get_visible() {
            continue;
        }
        lines.push(format!(
            "  group {:?} {:?}",
            group.title(),
            group.description().unwrap_or_default()
        ));
        for row in all::<adw::PreferencesRow>(&group) {
            if !row.get_visible() {
                continue;
            }
            let line = if let Some(switch) = row.downcast_ref::<adw::SwitchRow>() {
                format!("switch {:?} active={}", switch.title(), switch.is_active())
            } else if let Some(combo) = row.downcast_ref::<adw::ComboRow>() {
                let items: Vec<String> = combo
                    .model()
                    .map(|model| {
                        (0..model.n_items())
                            .filter_map(|i| model.item(i)?.downcast::<gtk::StringObject>().ok())
                            .map(|item| item.string().to_string())
                            .collect()
                    })
                    .unwrap_or_default();
                format!(
                    "combo {:?} selected={} items={items:?}",
                    combo.title(),
                    combo.selected()
                )
            } else if let Some(entry) = row.downcast_ref::<adw::EntryRow>() {
                format!("entry {:?} text={:?}", entry.title(), entry.text())
            } else if let Some(action) = row.downcast_ref::<adw::ActionRow>() {
                let buttons: Vec<String> = all::<gtk::Button>(action)
                    .iter()
                    .filter(|button| button.get_visible() && button.label().is_some())
                    .map(|button| {
                        format!(
                            "{}({})",
                            button.label().unwrap_or_default(),
                            button_state(button)
                        )
                    })
                    .collect();
                format!(
                    "row {:?} {:?} {buttons:?}",
                    action.title(),
                    action.subtitle().unwrap_or_default()
                )
            } else {
                let labels: Vec<String> = all::<gtk::Label>(&row)
                    .iter()
                    .filter(|label| label.get_visible() && !label.label().is_empty())
                    .map(label_text)
                    .collect();
                let views: Vec<String> = all::<gtk::TextView>(&row)
                    .iter()
                    .map(|view| {
                        let buffer = view.buffer();
                        let (start, end) = buffer.bounds();
                        format!(
                            "{:?}",
                            buffer
                                .text(&start, &end, false)
                                .chars()
                                .take(60)
                                .collect::<String>()
                        )
                    })
                    .collect();
                format!("other {labels:?} views={views:?}")
            };
            lines.push(format!("    {line}"));
        }
        // Labels placed straight into a group (theme preview, info).
        for label in all::<gtk::Label>(&group) {
            if label.get_visible()
                && !label.label().is_empty()
                && label.ancestor(adw::PreferencesRow::static_type()).is_none()
                && label.ancestor(gtk::ListBox::static_type()).is_none()
                && !label.has_css_class("title")
                && !label.has_css_class("heading")
                && label
                    .parent()
                    .is_some_and(|parent| !parent.has_css_class("header"))
            {
                lines.push(format!("    label {:?}", label.label()));
            }
        }
    }
}

fn describe(widget: &gtk::Widget) -> String {
    if let Some(compact) = describe_compact(widget) {
        return compact;
    }
    let mut lines = Vec::new();
    let subtitle = all::<gtk::Label>(widget)
        .into_iter()
        .find(|label| label.has_css_class("title-4") && label.has_css_class("dim-label"))
        .map(|label| label.label().to_string())
        .unwrap_or_default();
    let mut line = format!("state subtitle={subtitle:?}");
    if let Some((new, conv, settings)) = header(widget) {
        line.push_str(&format!(
            " new={} conv={} settings={}",
            button_state(&new),
            button_state(&conv),
            button_state(&settings)
        ));
    }
    if let Some(input) = input(widget) {
        let buffer = input.view.buffer();
        let (start, end) = buffer.bounds();
        let send_icon = input.send.icon_name().unwrap_or_default();
        let mic_image = input
            .mic
            .child()
            .and_then(|child| child.downcast::<gtk::Image>().ok())
            .map(|image| image_state(&image))
            .unwrap_or_default();
        line.push_str(&format!(
            "\n  input text={:?} entry={} send={send_icon} mic={}[{mic_image}] screen={} spinner={}",
            buffer.text(&start, &end, true),
            if input.view.is_sensitive() { "on" } else { "off" },
            button_state(&input.mic),
            button_state(&input.screen),
            input.spinner.get_visible()
        ));
    }
    lines.push(line);

    let revealers = all::<gtk::Revealer>(widget);
    let shown: Vec<String> = revealers
        .iter()
        .map(|revealer| format!("{}", revealer.reveals_child()))
        .collect();
    let scrims: Vec<String> = all::<gtk::Box>(widget)
        .iter()
        .filter(|scrim| scrim.has_css_class("settings-scrim"))
        .map(|scrim| format!("{}", scrim.get_visible()))
        .collect();
    lines.push(format!("  revealers={shown:?} scrims={scrims:?}"));

    // The conversations list, wherever it is attached.
    for revealer in &revealers {
        let Some(page) = revealer.child() else {
            continue;
        };
        if !revealer.reveals_child() || all::<gtk::ListBox>(&page).is_empty() {
            continue;
        }
        if page.has_css_class("settings-sidebar") {
            continue;
        }
        lines.push(format!(
            "  conversations [{}] width={}",
            classes(&page),
            page.width_request()
        ));
        for toggle in all::<gtk::ToggleButton>(&page) {
            if toggle.is_mapped() {
                lines.push(format!(
                    "  filter {:?} {}",
                    toggle.label().unwrap_or_default(),
                    button_state(&toggle)
                ));
            }
        }
        for label in all::<gtk::Label>(&page) {
            if label.is_mapped() && label.ancestor(gtk::ListBox::static_type()).is_none() {
                lines.push(format!("  label {:?}", label.label()));
            }
        }
        if let Some(list) = all::<gtk::ListBox>(&page).first() {
            describe_rows(list, &mut lines);
        }
    }

    // The settings sidebar.
    for revealer in &revealers {
        let Some(sidebar) = revealer.child() else {
            continue;
        };
        if !revealer.reveals_child() || !sidebar.has_css_class("settings-sidebar") {
            continue;
        }
        let Some(stack) = all::<adw::ViewStack>(&sidebar).into_iter().next() else {
            continue;
        };
        let name = stack.visible_child_name().unwrap_or_default();
        let titles: Vec<String> = stack
            .pages()
            .iter::<adw::ViewStackPage>()
            .flatten()
            .map(|page| page.title().unwrap_or_default().to_string())
            .collect();
        lines.push(format!("  settings page={name} titles={titles:?}"));
        if let Some(page) = stack.visible_child() {
            describe_preferences(&page, &mut lines);
        }
    }

    // The chat.
    let chat = all::<gtk::ListBox>(widget)
        .into_iter()
        .find(|list| list.ancestor(gtk::Stack::static_type()).is_some());
    if let Some(chat) = chat {
        for row in children(&chat) {
            let Some(box_) = row.first_child() else {
                continue;
            };
            let mut parts = vec![format!("  [{}] [{}]", classes(&row), classes(&box_))];
            for label in all::<gtk::Label>(&box_) {
                if label.get_visible() && label.ancestor(gtk::Expander::static_type()).is_none() {
                    parts.push(format!("label={}", q(&label_text(&label))));
                }
            }
            for expander in all::<gtk::Expander>(&box_) {
                let titles: Vec<String> = expander
                    .label_widget()
                    .map(|header| all::<gtk::Label>(&header).iter().map(label_text).collect())
                    .unwrap_or_default();
                let spinners: Vec<bool> = all::<gtk::Spinner>(&expander)
                    .iter()
                    .map(|spinner| spinner.get_visible())
                    .collect();
                let texts: Vec<String> = expander
                    .child()
                    .map(|child| all::<gtk::Label>(&child).iter().map(label_text).collect())
                    .unwrap_or_default();
                parts.push(format!(
                    "reasoning expanded={} title={titles:?} spinner={spinners:?} text={texts:?}",
                    expander.is_expanded()
                ));
            }
            let pictures = all::<gtk::Picture>(&box_).len();
            if pictures > 0 {
                parts.push(format!("pictures={pictures}"));
            }
            lines.push(parts.join(" "));
        }
    }
    lines.join("\n")
}

/// How many rows the chat has.
fn chat_rows(widget: &gtk::Widget) -> usize {
    all::<gtk::ListBox>(widget)
        .into_iter()
        .find(|list| list.ancestor(gtk::Stack::static_type()).is_some())
        .map_or(0, |list| children(&list).len())
}

/// The compact voice window's bar buttons.
fn compact_buttons(widget: &gtk::Widget) -> Vec<gtk::Widget> {
    all::<gtk::Box>(widget)
        .into_iter()
        .find(|bar| bar.has_css_class("compact-voice-bar"))
        .map(|bar| children(&bar))
        .unwrap_or_default()
}

/// The compact voice window: its buttons and the status under them.
fn describe_compact(widget: &gtk::Widget) -> Option<String> {
    let buttons = compact_buttons(widget);
    if buttons.is_empty() {
        return None;
    }
    let mut lines = vec![format!(
        "compact {}",
        buttons
            .iter()
            .map(button_state)
            .collect::<Vec<_>>()
            .join(" | ")
    )];
    if let Some(status) = all::<gtk::Box>(widget)
        .into_iter()
        .find(|bar| bar.has_css_class("compact-voice-bar"))
        .and_then(|bar| bar.next_sibling())
    {
        let labels: Vec<String> = all::<gtk::Label>(&status)
            .iter()
            .filter(|label| label.is_visible())
            .map(label_text)
            .collect();
        let spinners: Vec<bool> = all::<gtk::Spinner>(&status)
            .iter()
            .map(|s| s.is_visible())
            .collect();
        lines.push(format!(
            "  status shown={} spinner={spinners:?} labels={labels:?}",
            status.get_visible()
        ));
    }
    Some(lines.join("\n"))
}

fn first_mapped_button(root: &gtk::Widget, label: &str) -> Option<gtk::Button> {
    all::<gtk::Button>(root)
        .into_iter()
        .find(|button| button.label().as_deref() == Some(label) && button.is_mapped())
}

/// The ActionRow of a saved conversation.
fn conv_row(widget: &gtk::Widget, title: &str) -> Result<adw::ActionRow, String> {
    all::<adw::ActionRow>(widget)
        .into_iter()
        .find(|row| {
            row.title() == title
                && row.is_mapped()
                && row.ancestor(adw::PreferencesPage::static_type()).is_none()
        })
        .ok_or(format!("no conversation {title:?}"))
}

fn preferences_row<T: IsA<gtk::Widget> + IsA<adw::PreferencesRow>>(
    widget: &gtk::Widget,
    title: &str,
) -> Result<T, String> {
    all::<T>(widget)
        .into_iter()
        .find(|row| row.upcast_ref::<adw::PreferencesRow>().title() == title && row.is_mapped())
        .ok_or(format!("no row {title:?}"))
}

#[allow(deprecated)]
async fn step(window: &gtk::Window, widget: &gtk::Widget, step: &str) -> Result<(), String> {
    let (name, value) = step.split_once('=').unwrap_or((step, ""));
    let input_row = || input(widget).ok_or("no input row");
    let header = || header(widget).ok_or("no header");
    match name {
        "type" => input_row()?.view.buffer().set_text(value),
        "send" => input_row()?.send.emit_clicked(),
        "wait-idle" => {
            sleep(Duration::from_millis(300)).await;
            until("the reply", || {
                input(widget)
                    .is_some_and(|input| !input.spinner.get_visible() && input.view.is_sensitive())
            })
            .await?
        }
        "wait-dialog" => until("a dialog", || !message_dialogs().is_empty()).await?,
        "wait-window" => until("a progress window", || !progress_windows().is_empty()).await?,
        "wait-window-done" => {
            until("the progress window's end", || {
                progress_windows()
                    .iter()
                    .all(|window| window.is_deletable())
            })
            .await?
        }
        "answer" => {
            until("a dialog", || !message_dialogs().is_empty()).await?;
            sleep(Duration::from_millis(500)).await;
            print_dialogs();
            let dialog = message_dialogs().into_iter().next().ok_or("no dialog")?;
            match value.split_once('=') {
                Some((response, text)) => {
                    if let Some(entry) = all::<gtk::PasswordEntry>(&dialog).into_iter().next() {
                        entry.set_text(text);
                        dialog.response(response);
                        println!("answered {response} with a password");
                    } else {
                        let entry = all::<gtk::Entry>(&dialog)
                            .into_iter()
                            .next()
                            .ok_or("no entry in the dialog")?;
                        entry.set_text(text);
                        dialog.response(response);
                        println!("answered {response} with {text:?}");
                    }
                }
                None => {
                    dialog.response(value);
                    println!("answered {value}");
                }
            }
            sleep(Duration::from_millis(300)).await;
        }
        "new" => header()?.0.emit_clicked(),
        "conversations" => {
            let toggle = header()?.1;
            toggle.set_active(!toggle.is_active());
        }
        "conv-row" => {
            let row = conv_row(widget, value)?;
            row.emit_by_name::<()>("activated", &[]);
        }
        "conv-edit" | "conv-delete" => {
            let row = conv_row(widget, value)?;
            let buttons = all::<gtk::Button>(&row);
            let button = if name == "conv-edit" {
                buttons.first()
            } else {
                buttons.get(1)
            };
            button.ok_or("no row button")?.emit_clicked();
        }
        "rename" => {
            let entry = all::<adw::EntryRow>(widget)
                .into_iter()
                .find(|row| {
                    row.is_mapped() && row.ancestor(adw::PreferencesPage::static_type()).is_none()
                })
                .ok_or("no rename row")?;
            entry.set_text(value);
            entry.emit_by_name::<()>("entry-activated", &[]);
        }
        "filter" => {
            let toggle = all::<gtk::ToggleButton>(widget)
                .into_iter()
                .find(|toggle| toggle.label().as_deref() == Some(value) && toggle.is_mapped())
                .ok_or(format!("no filter {value:?}"))?;
            toggle.set_active(true);
        }
        "settings" => header()?.2.emit_clicked(),
        "settings-page" => {
            let stack = all::<adw::ViewStack>(widget)
                .into_iter()
                .next()
                .ok_or("no settings")?;
            stack.set_visible_child_name(value);
        }
        "close-settings" => {
            let sidebar = all::<gtk::Box>(widget)
                .into_iter()
                .find(|header| {
                    header.has_css_class("settings-sidebar-header") && header.is_mapped()
                })
                .ok_or("no settings sidebar")?;
            all::<gtk::Button>(&sidebar)
                .into_iter()
                .next()
                .ok_or("no close button")?
                .emit_clicked();
        }
        "switch" => {
            let row = preferences_row::<adw::SwitchRow>(widget, value)?;
            row.set_active(!row.is_active());
        }
        "combo" => {
            let (title, index) = value.rsplit_once('=').ok_or("combo=TITLE=INDEX")?;
            let row = preferences_row::<adw::ComboRow>(widget, title)?;
            row.set_selected(index.parse().map_err(|_| "bad index")?);
        }
        "entryrow" => {
            let (title, text) = value.split_once('=').ok_or("entryrow=TITLE=TEXT")?;
            let row = preferences_row::<adw::EntryRow>(widget, title)?;
            row.set_text(text);
        }
        "click" => {
            first_mapped_button(widget, value)
                .or_else(|| {
                    progress_windows()
                        .iter()
                        .find_map(|window| first_mapped_button(window.upcast_ref(), value))
                })
                .ok_or(format!("no button {value:?}"))?
                .emit_clicked();
        }
        "screen" => {
            let toggle = input_row()?.screen;
            toggle.set_active(!toggle.is_active());
        }
        "mic" => {
            let toggle = input_row()?.mic;
            toggle.set_active(!toggle.is_active());
        }
        "expand" => {
            for expander in all::<gtk::Expander>(widget) {
                expander.set_expanded(true);
            }
        }
        "prompt" => {
            let view = all::<gtk::TextView>(widget)
                .into_iter()
                .find(|view| {
                    view.is_editable()
                        && view.is_mapped()
                        && view.ancestor(adw::PreferencesPage::static_type()).is_some()
                })
                .ok_or("no editable prompt")?;
            view.buffer().set_text(value);
        }
        "wait-rows" => {
            let wanted: usize = value.parse().map_err(|_| "wait-rows=N")?;
            until("chat rows", || chat_rows(widget) >= wanted).await?
        }
        "close-window" => {
            let progress = progress_windows()
                .into_iter()
                .next()
                .ok_or("no progress window")?;
            progress.close();
        }
        "compact" => {
            let button = compact_buttons(widget)
                .into_iter()
                .find(|button| button.tooltip_text().as_deref() == Some(value))
                .ok_or(format!("no compact button {value:?}"))?;
            match button.downcast::<gtk::ToggleButton>() {
                Ok(toggle) => toggle.set_active(!toggle.is_active()),
                Err(button) => button
                    .downcast::<gtk::Button>()
                    .map_err(|_| "not a button")?
                    .emit_clicked(),
            }
        }
        "settle" => sleep(SETTLE).await,
        "sleep" => sleep(Duration::from_secs_f64(value.parse().unwrap_or(0.5))).await,
        "print" => {
            println!("{}", describe(widget));
            print_dialogs();
        }
        "shot" => render(window, Path::new(value))?,
        "dialog-shot" => {
            let dialog = message_dialogs()
                .into_iter()
                .next()
                .ok_or("no dialog to render")?;
            render(&dialog, Path::new(value))?;
        }
        "window-shot" => {
            let progress = progress_windows()
                .into_iter()
                .next()
                .ok_or("no progress window")?;
            render(&progress, Path::new(value))?;
        }
        "files" => print_files(),
        other => return Err(format!("unknown step {other:?}")),
    }
    Ok(())
}

/// The config file and the saved conversations (ids, times and the master
/// prompt shortened by examples/compare.sh).
fn print_files() {
    let home = PathBuf::from(std::env::var_os("HOME").unwrap_or_default());
    let config = home.join(".config/linexin");
    let text = std::fs::read_to_string(config.join("ai-sysadmin.json"))
        .unwrap_or_else(|_| "(none)".into());
    println!("config {text}");
    let mut names: Vec<String> = std::fs::read_dir(config.join("conversations"))
        .map(|entries| {
            entries
                .flatten()
                .map(|entry| entry.file_name().to_string_lossy().into_owned())
                .collect()
        })
        .unwrap_or_default();
    names.sort();
    let autostart = home.join(".config/autostart/hey-linux.desktop");
    if let Ok(text) = std::fs::read_to_string(autostart) {
        println!("autostart {text:?}");
    }
    for name in names {
        let text =
            std::fs::read_to_string(config.join("conversations").join(&name)).unwrap_or_default();
        println!("conversation {name}\n{text}");
    }
}

async fn scenario(window: gtk::Window, widget: gtk::Widget) -> Result<(), String> {
    sleep(SETTLE).await;
    for current in &options().steps {
        step(&window, &widget, current).await?;
    }
    sleep(Duration::from_millis(300)).await;
    for dialog in message_dialogs() {
        dialog.response("cancel");
    }
    Ok(())
}

fn style(app: &adw::Application) -> Result<(), String> {
    let _ = app;
    let options = options();
    adw::StyleManager::default().set_color_scheme(if options.dark {
        adw::ColorScheme::ForceDark
    } else {
        adw::ColorScheme::ForceLight
    });
    // The Center's application style sheet (.dim-label, ...).
    let css = gtk::CssProvider::new();
    css.load_from_string(&match &options.css {
        Some(path) => {
            std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?
        }
        None => linexin_plugin_api::CENTER_STYLE_CSS.to_owned(),
    });
    let display = gtk::gdk::Display::default().ok_or("no display")?;
    gtk::style_context_add_provider_for_display(
        &display,
        &css,
        gtk::STYLE_PROVIDER_PRIORITY_APPLICATION,
    );
    Ok(())
}

fn activate(app: &adw::Application) -> Result<(), String> {
    style(app)?;
    let options = options();
    let descriptor = load_descriptor(&options.library)?;
    let text = |pointer| {
        unsafe { CStr::from_ptr(pointer) }
            .to_string_lossy()
            .into_owned()
    };
    println!(
        "widgetname={:?} icon={} id={}",
        text(descriptor.name),
        text(descriptor.icon),
        text(descriptor.widget_id)
    );

    let (window, widget): (gtk::Window, gtk::Widget) = if options.compact_window {
        let create = descriptor
            .create_compact_voice
            .ok_or("no compact voice window")?;
        let application: *mut gtk::ffi::GtkApplication =
            app.upcast_ref::<gtk::Application>().to_glib_none().0;
        let context = CompactVoiceContextV1 {
            abi_version: ABI_VERSION,
            struct_size: size_of::<CompactVoiceContextV1>() as u32,
            host: &HOST,
            application: application.cast(),
            voice_autostart: options.voice,
        };
        let pointer = unsafe { create(&context) };
        if pointer.is_null() {
            return Err("create_compact_voice returned null".into());
        }
        let window = unsafe { gtk::Window::from_glib_full(pointer.cast::<gtk::ffi::GtkWindow>()) };
        window.present();
        (window.clone(), window.upcast())
    } else {
        let window = adw::ApplicationWindow::new(app);
        window.set_default_size(options.width, options.height);
        let context = CreateContextV1 {
            abi_version: ABI_VERSION,
            struct_size: size_of::<CreateContextV1>() as u32,
            host: &HOST,
            window: window.upcast_ref::<gtk::Window>().as_ptr().cast(),
            hide_sidebar: false,
            voice_autostart: options.voice,
            conversation_id: options
                .conversation
                .as_ref()
                .map_or(std::ptr::null(), |id| id.as_ptr()),
        };
        let create = descriptor.create.ok_or("descriptor without create")?;
        let pointer = unsafe { create(&context) };
        if pointer.is_null() {
            return Err("create returned null".into());
        }
        let widget = unsafe { gtk::Widget::from_glib_full(pointer.cast::<gtk::ffi::GtkWidget>()) };
        window.set_content(Some(&widget));
        window.present();
        (window.upcast(), widget)
    };

    let app = app.clone();
    glib::MainContext::default().spawn_local(async move {
        if let Err(error) = scenario(window, widget).await {
            eprintln!("harness: {error}");
            FAILED.with(|failed| failed.replace(true));
        }
        app.quit();
    });
    Ok(())
}

thread_local! {
    static FAILED: RefCell<bool> = const { RefCell::new(false) };
}

fn main() -> ExitCode {
    // The stylesheet, for the Python reference to load the same one.
    if std::env::args().nth(1).as_deref() == Some("--print-css") {
        print!("{}", linexin_plugin_api::CENTER_STYLE_CSS);
        return ExitCode::SUCCESS;
    }
    match parse_options() {
        Ok(options) => {
            let _ = OPTIONS.set(options);
            install_localization();
        }
        Err(error) => {
            eprintln!("harness: {error}");
            return ExitCode::FAILURE;
        }
    }
    let app = adw::Application::builder()
        .flags(gio::ApplicationFlags::NON_UNIQUE)
        .build();
    app.connect_activate(|app| {
        if let Err(error) = activate(app) {
            eprintln!("harness: {error}");
            FAILED.with(|failed| failed.replace(true));
            app.quit();
        }
    });
    let status = app.run_with_args::<&str>(&[]);
    if status == glib::ExitCode::SUCCESS && !FAILED.with(|failed| *failed.borrow()) {
        ExitCode::SUCCESS
    } else {
        ExitCode::FAILURE
    }
}

/// `name` where cargo built this harness's library: `<target>/<profile>/`,
/// beside the `examples/` holding the harness (wherever CARGO_TARGET_DIR put it).
fn beside_the_examples(name: &str) -> PathBuf {
    std::env::current_exe()
        .ok()
        .and_then(|harness| Some(harness.parent()?.parent()?.join(name)))
        .unwrap_or_else(|| PathBuf::from(name))
}
