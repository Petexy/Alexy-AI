//! The original `CompactVoiceWindow`: a small floating pill (close, mic,
//! settings, expand) for `linexin-center -w aa-alexy-ai-widget --voice
//! --compact`, driving a hidden Alexy widget.

use crate::ui::{self, Alexy, Launch, Observer, guard, icon, set_button_icon, tr};
use adw::prelude::*;
use gtk::glib;
use serde_json::Value;
use std::cell::Cell;
use std::rc::{Rc, Weak};

const CSS: &str = "
    .compact-voice-window {
        background: transparent;
    }
    .compact-voice-window, .compact-voice-window > * {
        min-width: 0;
        min-height: 0;
    }
    .compact-voice-bar {
        background: alpha(@window_bg_color, 0.92);
        border-radius: 28px;
        border: 1px solid alpha(@borders, 0.35);
        padding: 6px 10px;
        box-shadow: 0 4px 16px alpha(black, 0.18), 0 1px 4px alpha(black, 0.10);
    }
    .compact-voice-bar button {
        border-radius: 50%;
        min-width: 40px;
        min-height: 40px;
        padding: 0;
    }
    .compact-voice-bar .compact-mic-btn {
        background: alpha(@accent_bg_color, 0.12);
        min-width: 48px;
        min-height: 48px;
    }
    .compact-voice-bar .compact-mic-btn:checked {
        background: @accent_bg_color;
        color: @accent_fg_color;
    }
    .compact-voice-bar .compact-mic-btn:disabled {
        opacity: 0.5;
    }
    .compact-status-label {
        font-size: 11px;
        margin-top: 2px;
        margin-bottom: 2px;
    }
    .compact-spinner {
        min-width: 16px;
        min-height: 16px;
    }
    ";

pub struct Compact {
    window: adw::Window,
    alexy: Rc<Alexy>,
    mic_btn: gtk::ToggleButton,
    status_box: gtk::Box,
    status_spinner: gtk::Spinner,
    status_spinner_label: gtk::Label,
    status_scroll: gtk::ScrolledWindow,
    status_label: gtk::Label,
    pending_mic: Cell<bool>,
}

/// The status texts the compact bar shows for the widget's placeholders.
fn placeholder_status(text: &str) -> Option<(String, bool)> {
    [
        ("Listening...", "Listening…", false),
        ("Listening... (speak now)", "Listening… (speak now)", false),
        ("Transcribing...", "Transcribing…", true),
        ("Loading Whisper model...", "Loading voice model…", true),
    ]
    .iter()
    .find(|(placeholder, _, _)| tr(placeholder) == text)
    .map(|(_, label, spinner)| (tr(label), *spinner))
}

/// Build the window for `application`.
pub fn build(application: Option<gtk::Application>, voice_autostart: bool) -> Rc<Compact> {
    let window = adw::Window::new();
    if let Some(application) = &application {
        window.set_application(Some(application));
    }
    window.set_title(Some("Alexy"));
    window.set_default_size(260, -1);
    window.set_size_request(260, -1);
    window.set_resizable(false);
    window.set_deletable(false);
    window.set_decorated(false);
    let provider = gtk::CssProvider::new();
    provider.load_from_string(CSS);
    gtk::style_context_add_provider_for_display(
        &WidgetExt::display(&window),
        &provider,
        gtk::STYLE_PROVIDER_PRIORITY_APPLICATION + 10,
    );
    window.add_css_class("compact-voice-window");

    let (root, alexy) = ui::build(
        linexin_plugin_api::host(),
        Some(window.clone().upcast()),
        Launch {
            voice_autostart: false,
            conversation_id: None,
        },
    );
    root.set_visible(false);
    // The hidden widget lives as long as the window.
    // SAFETY: only read back as gtk::Box under this key.
    unsafe { window.set_data("alexy-hidden-root", root) };
    if voice_autostart && alexy.config.borrow().compact_screen_awareness {
        alexy.screen_awareness_active.set(true);
        alexy.voice_autostart.set(true);
        alexy.screen_toggle.set_active(true);
    }

    let root_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
    let bar = gtk::Box::new(gtk::Orientation::Horizontal, 8);
    bar.add_css_class("compact-voice-bar");
    bar.set_halign(gtk::Align::Center);
    bar.set_valign(gtk::Align::Center);
    bar.set_margin_top(6);
    bar.set_margin_bottom(6);
    bar.set_margin_start(6);
    bar.set_margin_end(6);

    let close_btn = gtk::Button::new();
    set_button_icon(
        &close_btn,
        &["window-close-symbolic", "window-close", "dialog-close"],
        Some("✕"),
    );
    close_btn.add_css_class("flat");
    close_btn.set_tooltip_text(Some(&tr("Close")));
    bar.append(&close_btn);

    let mic_btn = gtk::ToggleButton::new();
    let mic_icon = gtk::Image::from_icon_name(&icon(&[
        "audio-input-microphone-symbolic",
        "audio-input-microphone",
    ]));
    if let Some(svg) = alexy.theme_svg("microphone-icon.svg") {
        mic_icon.set_from_file(Some(&svg));
    }
    mic_btn.set_child(Some(&mic_icon));
    mic_btn.add_css_class("compact-mic-btn");
    mic_btn.set_tooltip_text(Some(&tr("Listen")));
    bar.append(&mic_btn);

    let settings_btn = gtk::Button::new();
    set_button_icon(
        &settings_btn,
        &[
            "emblem-system-symbolic",
            "emblem-system",
            "preferences-system-symbolic",
            "preferences-system",
            "configure",
        ],
        Some("⚙"),
    );
    settings_btn.add_css_class("flat");
    settings_btn.set_tooltip_text(Some(&tr("Settings")));
    bar.append(&settings_btn);

    let expand_btn = gtk::Button::new();
    set_button_icon(
        &expand_btn,
        &[
            "view-fullscreen-symbolic",
            "view-fullscreen",
            "view-restore-symbolic",
            "view-restore",
        ],
        Some("⛶"),
    );
    expand_btn.add_css_class("flat");
    expand_btn.set_tooltip_text(Some(&tr("Expand chat")));
    bar.append(&expand_btn);
    root_box.append(&bar);

    let status_box = gtk::Box::new(gtk::Orientation::Vertical, 0);
    status_box.set_halign(gtk::Align::Fill);
    status_box.set_margin_start(12);
    status_box.set_margin_end(12);
    status_box.set_margin_bottom(4);
    status_box.set_visible(false);
    let spinner_row = gtk::Box::new(gtk::Orientation::Horizontal, 6);
    spinner_row.set_halign(gtk::Align::Center);
    let status_spinner = gtk::Spinner::new();
    status_spinner.add_css_class("compact-spinner");
    status_spinner.set_visible(false);
    spinner_row.append(&status_spinner);
    let status_spinner_label = gtk::Label::new(Some(""));
    status_spinner_label.add_css_class("compact-status-label");
    status_spinner_label.add_css_class("dim-label");
    status_spinner_label.set_visible(false);
    spinner_row.append(&status_spinner_label);
    status_box.append(&spinner_row);
    let status_scroll = gtk::ScrolledWindow::new();
    status_scroll.set_policy(gtk::PolicyType::Never, gtk::PolicyType::Automatic);
    status_scroll.set_max_content_height(140);
    status_scroll.set_propagate_natural_height(true);
    status_scroll.set_visible(false);
    status_scroll.set_halign(gtk::Align::Fill);
    status_scroll.set_hexpand(true);
    let status_label = gtk::Label::new(Some(""));
    status_label.add_css_class("compact-status-label");
    status_label.add_css_class("dim-label");
    status_label.set_wrap(true);
    status_label.set_wrap_mode(gtk::pango::WrapMode::WordChar);
    status_label.set_max_width_chars(44);
    status_label.set_selectable(true);
    status_label.set_xalign(0.5);
    status_label.set_halign(gtk::Align::Fill);
    status_scroll.set_child(Some(&status_label));
    status_box.append(&status_scroll);
    root_box.append(&status_box);
    window.set_content(Some(&root_box));

    let compact = Rc::new(Compact {
        window: window.clone(),
        alexy: alexy.clone(),
        mic_btn: mic_btn.clone(),
        status_box,
        status_spinner,
        status_spinner_label,
        status_scroll,
        status_label,
        pending_mic: Cell::new(false),
    });
    alexy.observer.replace(Some(
        Rc::new(Watch(Rc::downgrade(&compact))) as Rc<dyn Observer>
    ));

    let weak = Rc::downgrade(&compact);
    close_btn.connect_clicked(move |_| {
        if let Some(compact) = weak.upgrade() {
            guard("close", || compact.on_close_clicked());
        }
    });
    let weak = Rc::downgrade(&compact);
    mic_btn.connect_toggled(move |button| {
        if let Some(compact) = weak.upgrade() {
            let active = button.is_active();
            guard("mic", || compact.on_mic_toggled(active));
        }
    });
    let weak = Rc::downgrade(&alexy);
    settings_btn.connect_clicked(move |_| {
        if let Some(alexy) = weak.upgrade() {
            guard("settings", || alexy.on_settings_clicked());
        }
    });
    let weak = Rc::downgrade(&compact);
    expand_btn.connect_clicked(move |_| {
        if let Some(compact) = weak.upgrade() {
            guard("expand", || compact.on_expand_clicked());
        }
    });
    let weak = Rc::downgrade(&compact);
    alexy.stt_toggle.connect_toggled(move |toggle| {
        if let Some(compact) = weak.upgrade()
            && compact.mic_btn.is_active() != toggle.is_active()
        {
            compact.mic_btn.set_active(toggle.is_active());
        }
    });
    let weak = Rc::downgrade(&compact);
    alexy.stt_toggle.connect_sensitive_notify(move |toggle| {
        if let Some(compact) = weak.upgrade() {
            compact.on_widget_stt_sensitivity_changed(toggle.is_sensitive());
        }
    });

    if voice_autostart && alexy.config.borrow().stt_backend == "whisper" {
        compact.preload_whisper_model();
    } else if voice_autostart {
        let mic = compact.mic_btn.downgrade();
        glib::idle_add_local_once(move || {
            if let Some(mic) = mic.upgrade() {
                mic.set_active(true);
            }
        });
    }
    compact
}

/// The hidden widget's events, for the bar.
struct Watch(Weak<Compact>);

impl Observer for Watch {
    fn bubble(&self, role: &str, content: &Value) {
        if let Some(compact) = self.0.upgrade()
            && role == "assistant"
        {
            let text = crate::conversations::extract_text(content);
            if !text.is_empty() {
                let shown: String = crate::conversations::python_strip(&text)
                    .chars()
                    .take(500)
                    .collect();
                compact.show_response(&shown);
            }
        }
    }

    fn placeholder(&self, text: &str) {
        let Some(compact) = self.0.upgrade() else {
            return;
        };
        if let Some((label, spinner)) = placeholder_status(text) {
            compact.show_status(&label, spinner);
        } else if text == tr("Ask a question...") {
            let current = compact.status_spinner_label.label().to_string();
            let transient = [
                "Listening…",
                "Listening… (speak now)",
                "Transcribing…",
                "Loading voice model…",
            ]
            .iter()
            .any(|status| tr(status) == current);
            if transient {
                compact.hide_status();
            }
        }
    }

    fn thinking_shown(&self) {
        if let Some(compact) = self.0.upgrade() {
            compact.show_status(&tr("Thinking…"), true);
        }
    }

    fn thinking_removed(&self) {
        if let Some(compact) = self.0.upgrade() {
            compact.status_spinner.stop();
            compact.status_spinner.set_visible(false);
            compact.status_spinner_label.set_visible(false);
        }
    }

    fn tts_started(&self) {
        if let Some(compact) = self.0.upgrade() {
            compact.mic_btn.set_sensitive(true);
        }
    }

    fn whisper_loading(&self) {
        if let Some(compact) = self.0.upgrade() {
            compact.show_status(&tr("Loading voice model…"), true);
        }
    }

    fn whisper_loaded(&self, ok: bool) {
        if let Some(compact) = self.0.upgrade() {
            if ok {
                compact.hide_status();
            } else {
                compact.show_status(&tr("Model load failed"), false);
            }
        }
    }
}

impl Compact {
    pub fn window(&self) -> &adw::Window {
        &self.window
    }

    /// SIGUSR1: re-toggle the mic (the original's `_retoggle(_mic_btn)`).
    pub fn reactivate(&self) {
        if self.mic_btn.is_active() {
            self.mic_btn.set_active(false);
        }
        self.mic_btn.set_active(true);
    }

    fn preload_whisper_model(self: &Rc<Self>) {
        let model = self.alexy.config.borrow().whisper_model.clone();
        let cache = std::env::var_os("HOME")
            .map(std::path::PathBuf::from)
            .unwrap_or_default()
            .join(".cache/whisper");
        if self.alexy.voice.model_loaded(&model) || !cache.join(format!("{model}.pt")).exists() {
            let mic = self.mic_btn.downgrade();
            glib::idle_add_local_once(move || {
                if let Some(mic) = mic.upgrade() {
                    mic.set_active(true);
                }
            });
            return;
        }
        // Loading starts with the mic itself: the hidden widget loads the
        // model (the bar shows it) and starts listening when it is ready.
        let mic = self.mic_btn.downgrade();
        glib::idle_add_local_once(move || {
            if let Some(mic) = mic.upgrade() {
                mic.set_active(true);
            }
        });
    }

    fn show_status(&self, text: &str, spinner: bool) {
        self.status_box.set_visible(true);
        self.status_spinner_label.set_label(text);
        self.status_spinner_label.set_visible(true);
        self.status_scroll.set_visible(false);
        if spinner {
            self.status_spinner.set_visible(true);
            self.status_spinner.start();
        } else {
            self.status_spinner.stop();
            self.status_spinner.set_visible(false);
        }
    }

    fn show_response(&self, text: &str) {
        self.status_box.set_visible(true);
        self.status_spinner.stop();
        self.status_spinner.set_visible(false);
        self.status_spinner_label.set_visible(false);
        self.status_label.set_label(text);
        self.status_scroll.set_visible(true);
    }

    fn hide_status(&self) {
        self.status_box.set_visible(false);
        self.status_spinner.stop();
        self.status_spinner.set_visible(false);
        self.status_spinner_label.set_visible(false);
        self.status_scroll.set_visible(false);
    }

    fn on_close_clicked(&self) {
        if self.alexy.stt_toggle.is_active() {
            self.alexy.stt_toggle.set_active(false);
        }
        if self.alexy.tts_playing.get() {
            self.alexy.stop_tts();
        }
        self.alexy.save_conversation();
        crate::screen::cleanup_tmp();
        self.window.close();
    }

    fn on_mic_toggled(&self, active: bool) {
        if active {
            if self.alexy.tts_playing.get() {
                self.alexy.stop_tts();
            }
            if !self.alexy.stt_toggle.is_sensitive() {
                self.mic_btn.set_active(false);
                self.pending_mic.set(true);
                return;
            }
        }
        if self.alexy.stt_toggle.is_active() != active {
            self.alexy.stt_toggle.set_active(active);
        }
        if active {
            self.show_status(&tr("Listening…"), false);
        } else if self.status_spinner_label.label() == tr("Listening…") {
            self.hide_status();
        }
    }

    fn on_widget_stt_sensitivity_changed(&self, sensitive: bool) {
        if !sensitive && self.alexy.tts_playing.get() {
            self.mic_btn.set_sensitive(true);
            return;
        }
        self.mic_btn.set_sensitive(sensitive);
        if sensitive && self.pending_mic.replace(false) {
            let mic = self.mic_btn.downgrade();
            glib::idle_add_local_once(move || {
                if let Some(mic) = mic.upgrade() {
                    mic.set_active(true);
                }
            });
        }
    }

    /// Save, open the full widget on this conversation, close.
    fn on_expand_clicked(&self) {
        self.alexy.save_conversation();
        let id = self.alexy.conversation_id.borrow().clone();
        if self.alexy.stt_toggle.is_active() {
            self.alexy.stt_toggle.set_active(false);
        }
        if self.alexy.tts_playing.get() {
            self.alexy.stop_tts();
        }
        let center = std::env::var_os("PATH")
            .and_then(|path| {
                std::env::split_paths(&path)
                    .map(|dir| dir.join("linexin-center"))
                    .find(|candidate| executable(candidate))
            })
            .or_else(|| {
                let library = crate::widgets_dir();
                ["../../../bin/linexin-center", "../../bin/linexin-center"]
                    .iter()
                    .map(|relative| library.join(relative))
                    .find(|candidate| candidate.is_file())
            })
            .and_then(|path| std::fs::canonicalize(path).ok());
        if let Some(center) = center {
            use std::os::unix::process::CommandExt;
            let mut command = std::process::Command::new(center);
            command
                .args(["-w", "aa-alexy-ai-widget", "--conversation", &id])
                .env("LINEXIN_NEW_INSTANCE", "1")
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null());
            // SAFETY: setsid is async-signal-safe (start_new_session=True).
            unsafe {
                command.pre_exec(|| {
                    libc::setsid();
                    Ok(())
                });
            }
            if let Ok(mut child) = command.spawn() {
                std::thread::spawn(move || child.wait());
            }
        }
        self.window.close();
    }
}

/// What `shutil.which` accepts: a file someone may execute.
fn executable(path: &std::path::Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path)
        .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
}
