//! The GTK interface: a port of `LinexinAISysadminWidget` (chat, input,
//! conversations, streaming replies). Settings live in `settings`, voice in
//! `voice`, speech in `tts`, screenshots in `screen`.

use crate::config::{self, Config, DEFAULT_AGENT_NAME};
use crate::conversations::{self, python_strip};
use crate::entry::MultilineEntry;
use crate::markdown;
use crate::theme::{self, Theme};
use crate::worker::{Settings, Shared, ToUi, Turn};
use adw::prelude::*;
use base64::Engine;
use futures_util::StreamExt;
use gtk::{gdk, gio, glib};
use linexin_plugin_api::{self as api, Host};
use regex::Regex;
use serde_json::{Value, json};
use std::cell::{Cell, RefCell};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::atomic::Ordering;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::Duration;

/// Translation domain (the dictionaries' `aa_alexy_ai`).
pub const DOMAIN: &str = "aa_alexy_ai";

pub fn tr(text: &str) -> String {
    api::tr(DOMAIN, text)
}

/// GTK calls back through C, where a panic must not unwind.
pub fn guard(what: &str, callback: impl FnOnce()) {
    if catch_unwind(AssertUnwindSafe(callback)).is_err() {
        eprintln!("Alexy AI: {what} panicked");
    }
}

/// `_resolve_icon`: the first icon the theme has.
pub fn resolve_icon(candidates: &[&str]) -> Option<String> {
    let display = gdk::Display::default()?;
    let theme = gtk::IconTheme::for_display(&display);
    candidates
        .iter()
        .find(|name| !name.is_empty() && theme.has_icon(name))
        .map(|name| name.to_string())
}

/// `_icon`: the first available, else the first.
pub fn icon(candidates: &[&str]) -> String {
    resolve_icon(candidates).unwrap_or_else(|| candidates[0].to_owned())
}

/// `_set_button_icon`.
pub fn set_button_icon(
    button: &impl IsA<gtk::Button>,
    candidates: &[&str],
    fallback: Option<&str>,
) {
    let button = button.as_ref();
    match (resolve_icon(candidates), fallback) {
        (Some(name), _) => button.set_icon_name(&name),
        (None, Some(text)) => button.set_label(text),
        (None, None) => button.set_icon_name(candidates[0]),
    }
}

/// Something the compact voice window wants to hear about (the original
/// replaced the hidden widget's methods to watch these).
pub trait Observer {
    fn bubble(&self, role: &str, content: &Value);
    fn placeholder(&self, text: &str);
    fn thinking_shown(&self);
    fn thinking_removed(&self);
    fn tts_started(&self);
    fn whisper_loading(&self);
    fn whisper_loaded(&self, ok: bool);
}

/// A streaming reply's bubble and typewriter state.
pub struct StreamCtx {
    row: gtk::ListBoxRow,
    bubble: gtk::Box,
    content_label: gtk::Label,
    reasoning: RefCell<Option<Reasoning>>,
    raw_content: RefCell<String>,
    reasoning_field: RefCell<String>,
    content_target: RefCell<Vec<char>>,
    reasoning_target: RefCell<Vec<char>>,
    content_shown: Cell<usize>,
    reasoning_shown: Cell<usize>,
    done: Cell<bool>,
    reply: RefCell<String>,
    speak: Cell<bool>,
    final_: Cell<bool>,
    finished: Cell<bool>,
}

struct Reasoning {
    label: gtk::Label,
    spinner: gtk::Spinner,
    title: gtk::Label,
}

pub struct Alexy {
    pub host: Option<Host>,
    pub window: Option<glib::WeakRef<gtk::Window>>,
    pub root: glib::WeakRef<gtk::Box>,
    pub config: RefCell<Config>,
    pub shared: Arc<Shared>,
    pub conversation_id: RefCell<String>,
    pub conv_created: RefCell<Option<String>>,
    pub conv_agent: RefCell<String>,
    pub theme: RefCell<Option<Theme>>,
    pub alexy_icon_path: PathBuf,
    pub widgeticon: String,
    pub llm_processing: Cell<bool>,
    pub tts_playing: Cell<bool>,
    pub tts_process: RefCell<Option<std::process::Child>>,
    stream: RefCell<Option<Rc<StreamCtx>>>,
    stream_tick: RefCell<Option<glib::SourceId>>,
    last_bubble_role: RefCell<Option<String>>,
    last_bubble_box: RefCell<Option<gtk::Box>>,
    thinking_row: RefCell<Option<gtk::ListBoxRow>>,
    pub pending_images: RefCell<Vec<(String, String)>>,
    pub screen_awareness_active: Cell<bool>,
    pub voice_autostart: Cell<bool>,
    pub last_input_was_voice: Cell<bool>,
    conv_active_filter: RefCell<Option<String>>,
    pub settings_apply: RefCell<Option<Box<dyn FnOnce()>>>,
    pub dynamic_models: RefCell<Vec<String>>,
    pub observer: RefCell<Option<Rc<dyn Observer>>>,
    pub voice: crate::voice::Voice,
    pub icon_send: String,
    pub icon_stop: String,

    pub subtitle_label: gtk::Label,
    pub header_icon: gtk::Image,
    pub new_conv_btn: gtk::Button,
    pub conv_toggle_btn: gtk::ToggleButton,
    pub settings_btn: gtk::Button,
    content_box: gtk::Box,
    pub main_stack: gtk::Stack,
    conv_page: gtk::Box,
    conv_scrolled: gtk::ScrolledWindow,
    conv_listbox: gtk::ListBox,
    conv_empty_label: gtk::Label,
    conv_sidebar_revealer: gtk::Revealer,
    conv_overlay_revealer: gtk::Revealer,
    conv_scrim: gtk::Box,
    conv_filter_box: RefCell<Option<gtk::Box>>,
    pub chat_listbox: gtk::ListBox,
    scrolled_window: gtk::ScrolledWindow,
    image_preview_box: gtk::Box,
    pub entry: MultilineEntry,
    pub send_btn: gtk::Button,
    pub stt_toggle: gtk::ToggleButton,
    pub stt_icon: gtk::Image,
    pub screen_toggle: gtk::ToggleButton,
    spinner: gtk::Spinner,
    pub settings_scrim: gtk::Box,
    pub settings_revealer: gtk::Revealer,
}

/// What the widget was started with.
#[derive(Clone, Default)]
pub struct Launch {
    pub voice_autostart: bool,
    pub conversation_id: Option<String>,
}

/// Build the widget (GTK main thread): the root box and its state.
pub fn build(
    host: Option<Host>,
    window: Option<gtk::Window>,
    launch: Launch,
) -> (gtk::Box, Rc<Alexy>) {
    config::migrate_legacy_config();
    let root = gtk::Box::new(gtk::Orientation::Vertical, 12);
    root.add_css_class("alexy-ai-root");
    let beside = crate::widgets_dir().join("../../icons/github.petexy.alexy.svg");
    let alexy_icon_path = if beside.is_file() {
        beside
    } else {
        PathBuf::from("/usr/share/icons/github.petexy.alexy.svg")
    };
    let widgeticon = if alexy_icon_path.is_file() {
        alexy_icon_path.to_string_lossy().into_owned()
    } else {
        "utilities-terminal-symbolic".into()
    };
    root.set_margin_top(4);
    root.set_margin_bottom(50);
    root.set_margin_start(50);
    root.set_margin_end(50);

    let mut config = Config::new();
    config.load();
    let history = vec![json!({"role": "system", "content": config.system_prompt})];
    let conv_agent = config.active_agent.clone();
    let alexy = Rc::new(Alexy {
        host,
        window: window.map(|window| window.downgrade()),
        root: root.downgrade(),
        config: RefCell::new(config),
        shared: Arc::new(Shared {
            history: Mutex::new(history),
            abort: Default::default(),
            speak_next: Default::default(),
        }),
        conversation_id: RefCell::new(new_id()),
        conv_created: RefCell::default(),
        conv_agent: RefCell::new(conv_agent),
        theme: RefCell::default(),
        alexy_icon_path,
        widgeticon,
        llm_processing: Cell::new(false),
        tts_playing: Cell::new(false),
        tts_process: RefCell::default(),
        stream: RefCell::default(),
        stream_tick: RefCell::default(),
        last_bubble_role: RefCell::default(),
        last_bubble_box: RefCell::default(),
        thinking_row: RefCell::default(),
        pending_images: RefCell::default(),
        screen_awareness_active: Cell::new(false),
        voice_autostart: Cell::new(launch.voice_autostart),
        last_input_was_voice: Cell::new(false),
        conv_active_filter: RefCell::default(),
        settings_apply: RefCell::default(),
        dynamic_models: RefCell::default(),
        observer: RefCell::default(),
        voice: crate::voice::Voice::default(),
        icon_send: icon(&[
            "mail-send-symbolic",
            "mail-send",
            "document-send-symbolic",
            "document-send",
            "go-next-symbolic",
        ]),
        icon_stop: icon(&[
            "media-playback-stop-symbolic",
            "media-playback-stop",
            "process-stop-symbolic",
            "process-stop",
        ]),
        subtitle_label: gtk::Label::new(None),
        header_icon: gtk::Image::new(),
        new_conv_btn: gtk::Button::new(),
        conv_toggle_btn: gtk::ToggleButton::new(),
        settings_btn: gtk::Button::new(),
        content_box: gtk::Box::new(gtk::Orientation::Vertical, 12),
        main_stack: gtk::Stack::new(),
        conv_page: gtk::Box::new(gtk::Orientation::Vertical, 8),
        conv_scrolled: gtk::ScrolledWindow::new(),
        conv_listbox: gtk::ListBox::new(),
        conv_empty_label: gtk::Label::new(Some(&tr("No saved conversations yet."))),
        conv_sidebar_revealer: gtk::Revealer::new(),
        conv_overlay_revealer: gtk::Revealer::new(),
        conv_scrim: gtk::Box::new(gtk::Orientation::Horizontal, 0),
        conv_filter_box: RefCell::default(),
        chat_listbox: gtk::ListBox::new(),
        scrolled_window: gtk::ScrolledWindow::new(),
        image_preview_box: gtk::Box::new(gtk::Orientation::Horizontal, 6),
        entry: MultilineEntry::new(),
        send_btn: gtk::Button::new(),
        stt_toggle: gtk::ToggleButton::new(),
        stt_icon: gtk::Image::new(),
        screen_toggle: gtk::ToggleButton::new(),
        spinner: gtk::Spinner::new(),
        settings_scrim: gtk::Box::new(gtk::Orientation::Horizontal, 0),
        settings_revealer: gtk::Revealer::new(),
    });
    alexy.reset_history();
    alexy.load_theme();
    alexy.setup_ui(&root);

    if let Some(id) = launch.conversation_id {
        let weak = Rc::downgrade(&alexy);
        glib::idle_add_local_once(move || {
            if let Some(alexy) = weak.upgrade() {
                guard("load conversation", || alexy.load_conversation(&id));
            }
        });
    }
    if launch.voice_autostart {
        println!("[Screen Awareness] voice_autostart=True, forcing screen awareness ON");
        let toggle = alexy.stt_toggle.downgrade();
        glib::idle_add_local_once(move || {
            if let Some(toggle) = toggle.upgrade() {
                toggle.set_active(true);
            }
        });
        alexy.screen_awareness_active.set(true);
        let toggle = alexy.screen_toggle.downgrade();
        glib::idle_add_local_once(move || {
            if let Some(toggle) = toggle.upgrade() {
                toggle.set_active(true);
            }
        });
    }
    (root, alexy)
}

fn new_id() -> String {
    glib::uuid_string_random().to_string()
}

impl Alexy {
    // ---------------------------------------------------------------- state

    /// `_reset_history`: a fresh conversation with the active agent's prompt.
    pub fn reset_history(&self) {
        let prompt = self.config.borrow().system_prompt.clone();
        if let Ok(mut history) = self.shared.history.lock() {
            *history = vec![json!({"role": "system", "content": prompt})];
        }
        self.conv_agent
            .replace(self.config.borrow().active_agent.clone());
    }

    pub fn history_len(&self) -> usize {
        self.shared.history.lock().map(|h| h.len()).unwrap_or(0)
    }

    /// `self.window if self.window else self.get_root()`.
    pub fn window_or_root(&self) -> Option<gtk::Window> {
        self.window
            .as_ref()
            .and_then(|window| window.upgrade())
            .or_else(|| {
                self.root
                    .upgrade()
                    .and_then(|root| root.root())
                    .and_then(|root| root.downcast::<gtk::Window>().ok())
            })
    }

    /// The injected `translate_dialog`.
    pub fn translate(&self, widget: &impl IsA<gtk::Widget>) {
        if let Some(host) = self.host {
            // SAFETY: a live GtkWidget on the GTK thread.
            unsafe { host.translate_widget(DOMAIN, widget.as_ref().as_ptr().cast()) };
        }
    }

    /// `_load_theme`.
    pub fn load_theme(&self) {
        let chosen = theme::choose(&self.config.borrow().theme);
        if let Some(theme) = &chosen {
            theme::apply(theme);
        }
        self.theme.replace(chosen);
    }

    /// `_get_theme_svg`.
    pub fn theme_svg(&self, name: &str) -> Option<PathBuf> {
        theme::svg(
            self.theme
                .borrow()
                .as_ref()
                .map(|theme| theme.path.as_path()),
            name,
        )
    }

    /// `update_subtitle`.
    pub fn update_subtitle(&self) {
        let config = self.config.borrow();
        let label = match config.backend.as_str() {
            "direct" => tr("Online API: {}").replacen("{}", &config.model, 1),
            "local" => tr("Local AI: {}").replacen("{}", &config.local_model, 1),
            "endpoint" => {
                let model = if config.endpoint_model.is_empty() {
                    tr("auto-detected")
                } else {
                    config.endpoint_model.clone()
                };
                tr("Local AI: {}").replacen("{}", &model, 1)
            }
            _ => return,
        };
        self.subtitle_label.set_label(&label);
    }

    // ------------------------------------------------------------------ UI

    fn connect_clicked(
        self: &Rc<Self>,
        button: &impl IsA<gtk::Button>,
        what: &'static str,
        then: fn(&Rc<Self>),
    ) {
        let weak = Rc::downgrade(self);
        button.as_ref().connect_clicked(move |_| {
            if let Some(alexy) = weak.upgrade() {
                guard(what, || then(&alexy));
            }
        });
    }

    fn setup_ui(self: &Rc<Self>, root: &gtk::Box) {
        let header_box = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        header_box.set_margin_bottom(8);
        if self.alexy_icon_path.is_file() {
            self.header_icon.set_from_file(Some(&self.alexy_icon_path));
        } else {
            self.header_icon.set_icon_name(Some("system-run-symbolic"));
        }
        self.header_icon.set_pixel_size(64);
        header_box.append(&self.header_icon);

        let title_box = gtk::Box::new(gtk::Orientation::Vertical, 4);
        title_box.set_hexpand(true);
        title_box.set_valign(gtk::Align::Center);
        let title_label = gtk::Label::new(Some(&tr("Alexy AI")));
        title_label.add_css_class("title-1");
        title_label.set_halign(gtk::Align::Start);
        title_box.append(&title_label);
        self.update_subtitle();
        self.subtitle_label.add_css_class("title-4");
        self.subtitle_label.add_css_class("dim-label");
        self.subtitle_label.set_halign(gtk::Align::Start);
        title_box.append(&self.subtitle_label);
        header_box.append(&title_box);

        let plus = gtk::Label::new(None);
        plus.set_markup("<span size=\"x-large\" weight=\"bold\">+</span>");
        self.new_conv_btn.set_child(Some(&plus));
        self.new_conv_btn.set_valign(gtk::Align::Center);
        self.new_conv_btn.add_css_class("circular");
        self.new_conv_btn
            .set_tooltip_text(Some(&tr("Start a new conversation")));
        self.connect_clicked(&self.new_conv_btn, "new conversation", |alexy| {
            alexy.on_new_conversation_clicked()
        });
        header_box.append(&self.new_conv_btn);

        set_button_icon(
            &self.conv_toggle_btn,
            &[
                "view-list-symbolic",
                "view-list",
                "view-list-details-symbolic",
                "view-list-details",
            ],
            Some("\u{2630}"),
        );
        self.conv_toggle_btn.set_valign(gtk::Align::Center);
        self.conv_toggle_btn.add_css_class("circular");
        self.conv_toggle_btn
            .set_tooltip_text(Some(&tr("Browse saved conversations")));
        let weak = Rc::downgrade(self);
        self.conv_toggle_btn.connect_toggled(move |button| {
            if let Some(alexy) = weak.upgrade() {
                let active = button.is_active();
                guard("conversations", || alexy.on_conversations_toggled(active));
            }
        });
        header_box.append(&self.conv_toggle_btn);

        set_button_icon(
            &self.settings_btn,
            &[
                "emblem-system-symbolic",
                "emblem-system",
                "preferences-system-symbolic",
                "preferences-system",
                "configure",
            ],
            Some("\u{2699}"),
        );
        self.settings_btn.set_valign(gtk::Align::Center);
        self.settings_btn.add_css_class("circular");
        self.connect_clicked(&self.settings_btn, "settings", |alexy| {
            alexy.on_settings_clicked()
        });
        header_box.append(&self.settings_btn);

        self.content_box.set_vexpand(true);
        self.content_box.append(&header_box);

        self.main_stack
            .set_transition_type(gtk::StackTransitionType::SlideLeftRight);
        self.main_stack.set_transition_duration(200);
        self.main_stack.set_vexpand(true);
        self.main_stack.set_hexpand(true);

        // Conversations page
        self.conv_scrolled
            .set_policy(gtk::PolicyType::Never, gtk::PolicyType::Automatic);
        self.conv_scrolled.set_vexpand(true);
        self.conv_listbox
            .set_selection_mode(gtk::SelectionMode::None);
        self.conv_listbox.add_css_class("boxed-list");
        self.conv_scrolled.set_child(Some(&self.conv_listbox));
        self.conv_page.append(&self.conv_scrolled);
        self.conv_empty_label.set_margin_top(40);
        self.conv_empty_label.add_css_class("dim-label");
        self.conv_empty_label.set_visible(false);
        self.conv_page.append(&self.conv_empty_label);

        let revealer = &self.conv_sidebar_revealer;
        revealer.set_transition_type(gtk::RevealerTransitionType::SlideRight);
        revealer.set_transition_duration(250);
        revealer.set_halign(gtk::Align::Start);
        revealer.set_valign(gtk::Align::Fill);
        revealer.set_reveal_child(false);
        revealer.set_child(Some(&self.conv_page));

        let content_split = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        content_split.set_vexpand(true);
        content_split.append(&self.conv_sidebar_revealer);
        content_split.append(&self.main_stack);
        self.content_box.append(&content_split);

        // Chat page
        let chat_page = gtk::Box::new(gtk::Orientation::Vertical, 0);
        self.chat_listbox
            .set_selection_mode(gtk::SelectionMode::None);
        self.chat_listbox.add_css_class("boxed-list");
        self.scrolled_window
            .set_policy(gtk::PolicyType::Never, gtk::PolicyType::Automatic);
        self.scrolled_window.set_child(Some(&self.chat_listbox));
        self.scrolled_window.set_vexpand(true);
        chat_page.append(&self.scrolled_window);

        self.image_preview_box.set_margin_top(6);
        self.image_preview_box.set_margin_start(4);
        self.image_preview_box.set_visible(false);
        chat_page.append(&self.image_preview_box);

        let input_box = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        input_box.set_margin_top(12);
        self.entry.set_placeholder_text(&tr("Ask a question..."));
        self.entry.scrolled.set_hexpand(true);
        let weak = Rc::downgrade(self);
        self.entry.connect_activate(move || {
            if let Some(alexy) = weak.upgrade() {
                guard("send", || alexy.on_send_clicked());
            }
        });
        input_box.append(&self.entry.scrolled);
        self.setup_image_input();

        self.send_btn.set_icon_name(&self.icon_send);
        self.send_btn.add_css_class("suggested-action");
        self.send_btn.set_size_request(40, 40);
        self.send_btn.set_valign(gtk::Align::End);
        self.connect_clicked(&self.send_btn, "send", |alexy| alexy.on_send_clicked());
        input_box.append(&self.send_btn);

        self.stt_icon.set_icon_name(Some(&icon(&[
            "audio-input-microphone-symbolic",
            "audio-input-microphone",
        ])));
        self.stt_toggle.set_child(Some(&self.stt_icon));
        self.stt_toggle.set_size_request(40, 40);
        self.stt_toggle.set_valign(gtk::Align::End);
        let weak = Rc::downgrade(self);
        self.stt_toggle.connect_toggled(move |button| {
            if let Some(alexy) = weak.upgrade() {
                let active = button.is_active();
                guard("voice", || alexy.on_stt_toggled(active));
            }
        });
        if let Some(svg) = self.theme_svg("microphone-icon.svg") {
            self.stt_icon.set_from_file(Some(&svg));
        }
        self.check_stt_availability();
        input_box.append(&self.stt_toggle);

        let screen_icon = gtk::Image::from_icon_name(&icon(&[
            "computer-symbolic",
            "computer",
            "video-display-symbolic",
            "video-display",
        ]));
        self.screen_toggle.set_child(Some(&screen_icon));
        self.screen_toggle.set_size_request(40, 40);
        self.screen_toggle.set_valign(gtk::Align::End);
        self.screen_toggle.set_tooltip_text(Some(&tr(
            "Screen Awareness: include a screenshot with your message",
        )));
        let weak = Rc::downgrade(self);
        self.screen_toggle.connect_toggled(move |button| {
            if let Some(alexy) = weak.upgrade() {
                let active = button.is_active();
                guard("screen", || alexy.on_screen_toggle(active));
            }
        });
        input_box.append(&self.screen_toggle);

        self.spinner.set_visible(false);
        input_box.append(&self.spinner);
        chat_page.append(&input_box);
        self.main_stack.add_named(&chat_page, Some("chat"));
        self.main_stack.set_visible_child_name("chat");

        let root_overlay = gtk::Overlay::new();
        root_overlay.set_child(Some(&self.content_box));

        self.conv_scrim.add_css_class("settings-scrim");
        self.conv_scrim.set_visible(false);
        let click = gtk::GestureClick::new();
        let toggle = self.conv_toggle_btn.downgrade();
        click.connect_released(move |_, _, _, _| {
            if let Some(toggle) = toggle.upgrade() {
                toggle.set_active(false);
            }
        });
        self.conv_scrim.add_controller(click);
        root_overlay.add_overlay(&self.conv_scrim);

        let revealer = &self.conv_overlay_revealer;
        revealer.set_transition_type(gtk::RevealerTransitionType::SlideRight);
        revealer.set_transition_duration(250);
        revealer.set_halign(gtk::Align::Start);
        revealer.set_valign(gtk::Align::Fill);
        revealer.set_reveal_child(false);
        root_overlay.add_overlay(revealer);

        self.settings_scrim.add_css_class("settings-scrim");
        self.settings_scrim.set_visible(false);
        let click = gtk::GestureClick::new();
        let weak = Rc::downgrade(self);
        click.connect_released(move |_, _, _, _| {
            if let Some(alexy) = weak.upgrade() {
                guard("close settings", || alexy.close_settings_sidebar());
            }
        });
        self.settings_scrim.add_controller(click);
        root_overlay.add_overlay(&self.settings_scrim);

        let revealer = &self.settings_revealer;
        revealer.set_transition_type(gtk::RevealerTransitionType::SlideLeft);
        revealer.set_transition_duration(250);
        revealer.set_halign(gtk::Align::End);
        revealer.set_valign(gtk::Align::Fill);
        revealer.set_reveal_child(false);
        root_overlay.add_overlay(revealer);

        root.append(&root_overlay);

        if let Some(window) = self.window.as_ref().and_then(|window| window.upgrade()) {
            let weak = Rc::downgrade(self);
            window.connect_notify_local(Some("default-width"), move |_, _| {
                if let Some(alexy) = weak.upgrade() {
                    guard("resize", || alexy.on_conv_window_resize());
                }
            });
        }
        let weak = Rc::downgrade(self);
        root.connect_map(move |_| {
            if let Some(alexy) = weak.upgrade() {
                guard("map", || alexy.on_conv_window_resize());
            }
        });
        let style_manager = adw::StyleManager::default();
        let weak = Rc::downgrade(self);
        style_manager.connect_dark_notify(move |manager| {
            if let Some(alexy) = weak.upgrade() {
                alexy.apply_dark(manager.is_dark());
            }
        });
        self.apply_dark(style_manager.is_dark());

        self.add_message_bubble(
            "assistant",
            &json!(tr("Hello! I am Alexy. How can I help you today?")),
        );
    }

    fn apply_dark(&self, dark: bool) {
        let widgets: [Option<gtk::Widget>; 3] = [
            self.root.upgrade().map(|root| root.upcast()),
            Some(self.main_stack.clone().upcast()),
            Some(self.chat_listbox.clone().upcast()),
        ];
        for widget in widgets.into_iter().flatten() {
            if dark {
                widget.add_css_class("dark");
            } else {
                widget.remove_css_class("dark");
            }
        }
    }

    /// The paste and drop handlers of the input.
    fn setup_image_input(self: &Rc<Self>) {
        let paste = gtk::EventControllerKey::new();
        paste.set_propagation_phase(gtk::PropagationPhase::Capture);
        let weak = Rc::downgrade(self);
        paste.connect_key_pressed(move |_, key, _, state| {
            let Some(alexy) = weak.upgrade() else {
                return glib::Propagation::Proceed;
            };
            if key == gdk::Key::v && state.contains(gdk::ModifierType::CONTROL_MASK) {
                let clipboard = alexy.entry.textview.clipboard();
                let formats = clipboard.formats();
                for mime in [
                    "image/png",
                    "image/jpeg",
                    "image/bmp",
                    "image/gif",
                    "image/tiff",
                ] {
                    if formats.contain_mime_type(mime) {
                        let weak = Rc::downgrade(&alexy);
                        clipboard.read_texture_async(None::<&gio::Cancellable>, move |result| {
                            match result {
                                Ok(Some(texture)) => {
                                    if let Some(alexy) = weak.upgrade() {
                                        alexy.add_image_from_texture(&texture);
                                    }
                                }
                                Ok(None) => {}
                                Err(error) => println!("[Image paste] Failed: {error}"),
                            }
                        });
                        return glib::Propagation::Stop;
                    }
                }
            }
            glib::Propagation::Proceed
        });
        self.entry.textview.add_controller(paste);

        let files = gtk::DropTarget::new(gdk::FileList::static_type(), gdk::DragAction::COPY);
        let weak = Rc::downgrade(self);
        files.connect_drop(move |_, value, _, _| {
            if let (Some(alexy), Ok(list)) = (weak.upgrade(), value.get::<gdk::FileList>()) {
                for file in list.files() {
                    if let Some(path) = file.path()
                        && is_image_file(&path)
                    {
                        alexy.add_image_from_file(&path);
                    }
                }
            }
            true
        });
        self.entry.scrolled.add_controller(files);

        let textures = gtk::DropTarget::new(gdk::Texture::static_type(), gdk::DragAction::COPY);
        let weak = Rc::downgrade(self);
        textures.connect_drop(move |_, value, _, _| {
            if let (Some(alexy), Ok(texture)) = (weak.upgrade(), value.get::<gdk::Texture>()) {
                alexy.add_image_from_texture(&texture);
            }
            true
        });
        self.entry.scrolled.add_controller(textures);
    }

    // ------------------------------------------------------- conversations

    pub fn clear_chat_ui(&self) {
        while let Some(row) = self.chat_listbox.row_at_index(0) {
            self.chat_listbox.remove(&row);
        }
        self.last_bubble_role.replace(None);
        self.last_bubble_box.replace(None);
    }

    /// `_save_conversation`.
    pub fn save_conversation(&self) {
        let history = self
            .shared
            .history
            .lock()
            .map(|h| h.clone())
            .unwrap_or_default();
        if history.len() <= 1 {
            return;
        }
        let created = self
            .conv_created
            .borrow_mut()
            .get_or_insert_with(conversations::now_iso)
            .clone();
        let config = self.config.borrow();
        conversations::save(
            &self.conversation_id.borrow(),
            &history,
            &created,
            &config.backend,
            &self.conv_agent.borrow(),
        );
    }

    /// `_load_conversation`.
    pub fn load_conversation(self: &Rc<Self>, id: &str) {
        let Some(saved) = conversations::load(id) else {
            return;
        };
        self.save_conversation();
        self.conversation_id.replace(saved.id);
        if let Ok(mut history) = self.shared.history.lock() {
            *history = saved.history.clone();
        }
        self.conv_created.replace(Some(saved.created));
        self.conv_agent.replace(saved.agent);
        let mut backend = saved
            .backend
            .unwrap_or_else(|| self.config.borrow().backend.clone());
        if backend == "qwen_cli" {
            backend = "direct".into();
        }
        if backend != self.config.borrow().backend {
            self.config.borrow_mut().backend = backend;
            self.update_subtitle();
        }
        self.clear_chat_ui();
        static COMMAND_ONLY: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"(?s)^```(?:bash|sh)\n.*?```$").expect("valid"));
        for message in &saved.history {
            let content = message.get("content").cloned().unwrap_or(Value::Null);
            match message.get("role").and_then(Value::as_str) {
                Some("user") => {
                    if conversations::extract_text(&content)
                        .starts_with("System Command Execution Results:")
                    {
                        continue;
                    }
                    self.add_message_bubble(
                        "user",
                        &conversations::strip_system_instructions(&content),
                    );
                }
                Some("assistant") => {
                    let text = conversations::extract_text(&content);
                    if COMMAND_ONLY.is_match(python_strip(&text)) {
                        continue;
                    }
                    self.add_message_bubble("assistant", &content);
                }
                _ => {}
            }
        }
    }

    fn greet(self: &Rc<Self>) {
        self.add_message_bubble(
            "assistant",
            &json!(tr("Hello! I am Alexy. How can I help you today?")),
        );
    }

    /// `on_new_conversation_clicked`.
    pub fn on_new_conversation_clicked(self: &Rc<Self>) {
        self.save_conversation();
        self.conversation_id.replace(new_id());
        self.conv_created.replace(None);
        self.reset_history();
        self.clear_chat_ui();
        self.greet();
    }

    fn on_conversations_toggled(self: &Rc<Self>, active: bool) {
        if active {
            self.rebuild_conv_list();
            self.show_conv();
        } else {
            self.hide_conv();
        }
    }

    /// `_should_use_conv_sidebar`: the Center compact and the widget wide.
    fn should_use_conv_sidebar(&self) -> bool {
        let compact = match (self.host, self.window.as_ref().and_then(|w| w.upgrade())) {
            (Some(host), Some(window)) => host.window_compact_mode(window.as_ptr().cast()),
            _ => false,
        };
        let width = self.root.upgrade().map_or(0, |root| root.width());
        compact && width > 1000
    }

    fn attach_conv(&self, sidebar: bool) {
        let (target, other) = if sidebar {
            (&self.conv_sidebar_revealer, &self.conv_overlay_revealer)
        } else {
            (&self.conv_overlay_revealer, &self.conv_sidebar_revealer)
        };
        let page: gtk::Widget = self.conv_page.clone().upcast();
        if target.child().as_ref() == Some(&page) {
            return;
        }
        if other.child().as_ref() == Some(&page) {
            other.set_reveal_child(false);
            other.set_child(None::<&gtk::Widget>);
        }
        target.set_child(Some(&page));
    }

    fn show_conv(self: &Rc<Self>) {
        if self.settings_revealer.reveals_child() {
            self.close_settings_sidebar();
        }
        if self.should_use_conv_sidebar() {
            self.attach_conv(true);
            self.conv_page.set_size_request(340, -1);
            self.conv_page.set_hexpand(false);
            self.conv_page.add_css_class("conv-sidebar");
            self.conv_page.remove_css_class("conv-panel");
            self.conv_scrim.set_visible(false);
            self.conv_overlay_revealer.set_reveal_child(false);
            self.conv_sidebar_revealer.set_reveal_child(true);
        } else {
            self.attach_conv(false);
            self.conv_page.set_size_request(360, -1);
            self.conv_page.set_hexpand(false);
            self.conv_page.add_css_class("conv-panel");
            self.conv_page.remove_css_class("conv-sidebar");
            self.conv_sidebar_revealer.set_reveal_child(false);
            self.conv_scrim.set_visible(true);
            self.conv_overlay_revealer.set_reveal_child(true);
        }
        self.main_stack.set_visible(true);
        self.new_conv_btn.set_sensitive(true);
    }

    fn hide_conv(&self) {
        self.conv_sidebar_revealer.set_reveal_child(false);
        self.conv_overlay_revealer.set_reveal_child(false);
        self.conv_scrim.set_visible(false);
        self.main_stack.set_visible(true);
        self.new_conv_btn.set_sensitive(true);
    }

    fn on_conv_window_resize(self: &Rc<Self>) {
        if self.conv_toggle_btn.is_active() {
            self.show_conv();
        }
    }

    /// `_rebuild_conv_list`: the backend filter bar, then the rows.
    fn rebuild_conv_list(self: &Rc<Self>) {
        let all = conversations::list(None);
        let mut available: Vec<String> = Vec::new();
        for conversation in &all {
            let backend = conversations::load(&conversation.id)
                .and_then(|saved| saved.backend)
                .unwrap_or_default();
            if !available.contains(&backend) {
                available.push(backend);
            }
        }
        if available.iter().any(|backend| backend == "qwen_cli") {
            available.retain(|backend| backend != "qwen_cli");
            if !available.iter().any(|backend| backend == "direct") {
                available.push("direct".into());
            }
        }
        let current_backend = self.config.borrow().backend.clone();
        if !available.contains(&current_backend) {
            available.push(current_backend.clone());
        }

        if let Some(old) = self.conv_filter_box.take()
            && old.parent().is_some()
        {
            self.conv_page.remove(&old);
        }
        let filter_box = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        filter_box.set_halign(gtk::Align::Center);
        filter_box.set_margin_bottom(4);
        self.conv_page.prepend(&filter_box);
        self.conv_filter_box.replace(Some(filter_box.clone()));

        if self.conv_active_filter.borrow().is_none() {
            self.conv_active_filter
                .replace(Some(current_backend.clone()));
        }
        if self.conv_active_filter.borrow().as_deref() == Some("qwen_cli") {
            self.conv_active_filter.replace(Some("direct".into()));
        }
        if available.len() > 1 {
            let label = gtk::Label::new(Some(&tr("Backend:")));
            label.add_css_class("dim-label");
            filter_box.append(&label);
            let group = gtk::Box::new(gtk::Orientation::Horizontal, 0);
            group.add_css_class("linked");
            let mut first: Option<gtk::ToggleButton> = None;
            for (key, name) in [
                ("direct", tr("Online API")),
                ("local", tr("Local AI")),
                ("endpoint", tr("Local AI (Endpoint)")),
            ] {
                if !available.iter().any(|backend| backend == key) {
                    continue;
                }
                let button = gtk::ToggleButton::with_label(&name);
                if self.conv_active_filter.borrow().as_deref() == Some(key) {
                    button.set_active(true);
                }
                match &first {
                    Some(first) => button.set_group(Some(first)),
                    None => first = Some(button.clone()),
                }
                let weak = Rc::downgrade(self);
                button.connect_toggled(move |button| {
                    if button.is_active()
                        && let Some(alexy) = weak.upgrade()
                    {
                        alexy.conv_active_filter.replace(Some(key.to_owned()));
                        guard("filter", || alexy.populate_conv_rows());
                    }
                });
                group.append(&button);
            }
            filter_box.append(&group);
            filter_box.set_visible(true);
        } else {
            filter_box.set_visible(false);
            self.conv_active_filter.replace(Some(current_backend));
        }
        self.populate_conv_rows();
    }

    /// `_populate_conv_rows`.
    fn populate_conv_rows(self: &Rc<Self>) {
        while let Some(row) = self.conv_listbox.row_at_index(0) {
            self.conv_listbox.remove(&row);
        }
        let filter = self.conv_active_filter.borrow().clone();
        let listed = conversations::list(filter.as_deref());
        self.conv_empty_label.set_visible(listed.is_empty());
        self.conv_scrolled.set_visible(!listed.is_empty());
        let mut last_agent: Option<String> = None;
        for conversation in listed {
            let agent = if conversation.agent.is_empty() {
                DEFAULT_AGENT_NAME.to_owned()
            } else {
                conversation.agent.clone()
            };
            if last_agent.as_ref() != Some(&agent) {
                last_agent = Some(agent.clone());
                let header_row = gtk::ListBoxRow::new();
                header_row.set_selectable(false);
                header_row.set_activatable(false);
                let header = gtk::Label::new(Some(&agent));
                header.add_css_class("heading");
                header.add_css_class("dim-label");
                header.set_halign(gtk::Align::Start);
                header.set_margin_top(10);
                header.set_margin_bottom(2);
                header.set_margin_start(6);
                header_row.set_child(Some(&header));
                self.conv_listbox.append(&header_row);
            }
            let row = adw::ActionRow::new();
            row.set_title(&conversation.title);
            row.set_subtitle(&conversations::display_time(&conversation.updated));
            if conversation.id == *self.conversation_id.borrow() {
                row.add_prefix(&gtk::Image::from_icon_name(&icon(&[
                    "emblem-ok-symbolic",
                    "emblem-ok",
                    "object-select-symbolic",
                    "object-select",
                ])));
            }

            let edit_btn = gtk::Button::new();
            set_button_icon(
                &edit_btn,
                &[
                    "document-edit-symbolic",
                    "document-edit",
                    "edit-rename-symbolic",
                    "edit-rename",
                ],
                Some("\u{270e}"),
            );
            edit_btn.set_valign(gtk::Align::Center);
            edit_btn.add_css_class("flat");
            edit_btn.set_focusable(false);
            {
                let weak = Rc::downgrade(self);
                let (id, title, row_weak) = (
                    conversation.id.clone(),
                    conversation.title.clone(),
                    row.downgrade(),
                );
                edit_btn.connect_clicked(move |_| {
                    if let (Some(alexy), Some(row)) = (weak.upgrade(), row_weak.upgrade()) {
                        guard("rename", || alexy.start_rename(&id, &title, &row));
                    }
                });
            }
            row.add_suffix(&edit_btn);

            let delete_btn = gtk::Button::new();
            set_button_icon(
                &delete_btn,
                &[
                    "user-trash-symbolic",
                    "user-trash",
                    "edit-delete-symbolic",
                    "edit-delete",
                ],
                Some("\u{1f5d1}"),
            );
            delete_btn.set_valign(gtk::Align::Center);
            delete_btn.add_css_class("flat");
            delete_btn.add_css_class("error");
            delete_btn.set_focusable(false);
            {
                let weak = Rc::downgrade(self);
                let id = conversation.id.clone();
                delete_btn.connect_clicked(move |_| {
                    if let Some(alexy) = weak.upgrade() {
                        guard("delete", || {
                            conversations::delete(&id);
                            if id == *alexy.conversation_id.borrow() {
                                alexy.conversation_id.replace(new_id());
                                alexy.conv_created.replace(None);
                                alexy.reset_history();
                                alexy.clear_chat_ui();
                                alexy.greet();
                            }
                            alexy.populate_conv_rows();
                        });
                    }
                });
            }
            row.add_suffix(&delete_btn);

            row.set_activatable(true);
            let weak = Rc::downgrade(self);
            let id = conversation.id.clone();
            row.connect_activated(move |_| {
                if let Some(alexy) = weak.upgrade() {
                    guard("open", || {
                        alexy.load_conversation(&id);
                        alexy.conv_toggle_btn.set_active(false);
                    });
                }
            });
            self.conv_listbox.append(&row);
        }
    }

    /// The edit button: the row becomes an entry for the new title.
    fn start_rename(self: &Rc<Self>, id: &str, title: &str, row: &adw::ActionRow) {
        let index = row.index();
        let edit_row = adw::EntryRow::new();
        edit_row.set_title(&tr("Rename conversation"));
        edit_row.set_text(title);
        edit_row.add_css_class("boxed-list");
        let cancel_btn = gtk::Button::new();
        set_button_icon(
            &cancel_btn,
            &["window-close-symbolic", "window-close", "dialog-close"],
            Some("\u{2715}"),
        );
        cancel_btn.set_valign(gtk::Align::Center);
        cancel_btn.add_css_class("flat");
        let weak = Rc::downgrade(self);
        cancel_btn.connect_clicked(move |_| {
            if let Some(alexy) = weak.upgrade() {
                guard("cancel rename", || alexy.populate_conv_rows());
            }
        });
        edit_row.add_suffix(&cancel_btn);
        let apply = {
            let weak = Rc::downgrade(self);
            let id = id.to_owned();
            move |entry: &adw::EntryRow| {
                if let Some(alexy) = weak.upgrade() {
                    let title = entry.text();
                    let title = python_strip(&title);
                    if !title.is_empty() {
                        conversations::rename(&id, title);
                    }
                    guard("rename", || alexy.populate_conv_rows());
                }
            }
        };
        let apply = Rc::new(apply);
        let on_apply = apply.clone();
        edit_row.connect_apply(move |entry| on_apply(entry));
        edit_row.connect_entry_activated(move |entry| apply(entry));
        self.conv_listbox.remove(row);
        self.conv_listbox.insert(&edit_row, index);
        edit_row.grab_focus();
    }

    // ------------------------------------------------------------ bubbles

    fn scroll_chat_to_bottom(&self) {
        let adjustment = self.scrolled_window.vadjustment();
        adjustment.set_value(adjustment.upper() - adjustment.page_size());
    }

    /// `add_message_bubble`.
    pub fn add_message_bubble(self: &Rc<Self>, role: &str, content: &Value) {
        let row = gtk::ListBoxRow::new();
        row.set_selectable(false);
        row.add_css_class("message-row");
        row.add_css_class(if role == "user" {
            "user-message-row"
        } else {
            "assistant-message-row"
        });
        if self.last_bubble_role.borrow().as_deref() == Some(role)
            && let Some(previous) = self.last_bubble_box.borrow().as_ref()
        {
            previous.remove_css_class("last-in-group");
        }
        let message_box = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        message_box.add_css_class("message-box");
        message_box.add_css_class("last-in-group");
        self.last_bubble_role.replace(Some(role.to_owned()));
        self.last_bubble_box.replace(Some(message_box.clone()));
        message_box.add_css_class(if role == "user" {
            "user-message-box"
        } else {
            "assistant-message-box"
        });

        let (text_content, images) = match content {
            Value::Array(_) => (
                conversations::extract_text(content),
                conversations::extract_images(content),
            ),
            Value::String(text) => (text.clone(), Vec::new()),
            other => (crate::pyrepr::str(other), Vec::new()),
        };
        let markup = markdown::to_pango(&text_content);

        if role == "user" {
            message_box.set_halign(gtk::Align::End);
            let bubble = gtk::Box::new(gtk::Orientation::Vertical, 6);
            bubble.add_css_class("message-bubble");
            bubble.add_css_class("user-bubble");
            if !images.is_empty() {
                let images_box = gtk::Box::new(gtk::Orientation::Horizontal, 4);
                images_box.set_halign(gtk::Align::End);
                for url in &images {
                    let data = url.split_once(',').map_or(url.as_str(), |(_, data)| data);
                    let Ok(raw) = base64::engine::general_purpose::STANDARD.decode(data) else {
                        continue;
                    };
                    let Ok(texture) = gdk::Texture::from_bytes(&glib::Bytes::from_owned(raw))
                    else {
                        continue;
                    };
                    let picture = gtk::Picture::for_paintable(&texture);
                    picture.set_size_request(150, 150);
                    picture.set_can_shrink(true);
                    picture.set_content_fit(gtk::ContentFit::Cover);
                    let frame = gtk::Frame::new(None);
                    frame.set_child(Some(&picture));
                    images_box.append(&frame);
                }
                bubble.append(&images_box);
            }
            if !python_strip(&text_content).is_empty() {
                let label = gtk::Label::new(None);
                label.add_css_class("message-label");
                label.set_markup(&markup);
                label.set_wrap(true);
                label.set_selectable(true);
                label.set_xalign(1.0);
                bubble.append(&label);
            }
            message_box.append(&bubble);
        } else {
            message_box.set_halign(gtk::Align::Start);
            message_box.append(&self.avatar());
            let label = gtk::Label::new(None);
            label.add_css_class("message-label");
            label.set_markup(&markup);
            label.set_wrap(true);
            label.set_selectable(true);
            label.set_xalign(0.0);
            let bubble = gtk::Box::new(gtk::Orientation::Horizontal, 0);
            bubble.add_css_class("message-bubble");
            bubble.add_css_class("assistant-bubble");
            bubble.append(&label);
            message_box.append(&bubble);
        }
        row.set_child(Some(&message_box));
        self.chat_listbox.append(&row);

        let weak = Rc::downgrade(self);
        glib::timeout_add_local_once(Duration::from_millis(100), move || {
            if let Some(alexy) = weak.upgrade() {
                alexy.scroll_chat_to_bottom();
            }
        });
        let adjustment = self.scrolled_window.vadjustment();
        glib::idle_add_local_once(move || {
            if adjustment.upper() > adjustment.page_size() {
                adjustment.set_value(adjustment.upper() - adjustment.page_size());
            }
        });
        let observer = self.observer.borrow().clone();
        if let Some(observer) = observer {
            observer.bubble(role, content);
        }
    }

    /// The assistant's 24-pixel avatar (never themed).
    fn avatar(&self) -> gtk::Image {
        let image = if self.alexy_icon_path.is_file() {
            gtk::Image::from_file(&self.alexy_icon_path)
        } else {
            gtk::Image::from_icon_name(&self.widgeticon)
        };
        image.set_pixel_size(24);
        image.set_valign(gtk::Align::Start);
        image
    }

    /// `_show_thinking_indicator`.
    pub fn show_thinking_indicator(self: &Rc<Self>) {
        self.remove_thinking_indicator_only();
        let row = gtk::ListBoxRow::new();
        row.set_selectable(false);
        row.add_css_class("message-row");
        row.add_css_class("assistant-message-row");
        row.add_css_class("thinking-row");
        let message_box = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        message_box.add_css_class("message-box");
        message_box.add_css_class("assistant-message-box");
        message_box.add_css_class("last-in-group");
        message_box.set_halign(gtk::Align::Start);
        if let Some(svg) = self.theme_svg("thinking-indicator.svg") {
            let avatar = gtk::Image::from_file(&svg);
            avatar.set_pixel_size(-1);
            let bubble = gtk::Box::new(gtk::Orientation::Horizontal, 0);
            bubble.add_css_class("message-bubble");
            bubble.add_css_class("assistant-bubble");
            bubble.add_css_class("thinking-bubble");
            bubble.append(&avatar);
            message_box.append(&bubble);
        } else {
            message_box.append(&self.avatar());
            let inner = gtk::Box::new(gtk::Orientation::Horizontal, 8);
            let spinner = gtk::Spinner::new();
            spinner.start();
            inner.append(&spinner);
            let label = gtk::Label::new(Some(&tr("Thinking...")));
            label.add_css_class("dim-label");
            inner.append(&label);
            message_box.append(&inner);
        }
        row.set_child(Some(&message_box));
        self.chat_listbox.append(&row);
        self.thinking_row.replace(Some(row));
        let weak = Rc::downgrade(self);
        glib::timeout_add_local_once(Duration::from_millis(100), move || {
            if let Some(alexy) = weak.upgrade() {
                alexy.scroll_chat_to_bottom();
            }
        });
        let observer = self.observer.borrow().clone();
        if let Some(observer) = observer {
            observer.thinking_shown();
        }
    }

    fn remove_thinking_indicator_only(&self) {
        self.last_bubble_role.replace(None);
        self.last_bubble_box.replace(None);
        if let Some(row) = self.thinking_row.take() {
            self.chat_listbox.remove(&row);
        }
    }

    /// `_remove_thinking_indicator`.
    pub fn remove_thinking_indicator(&self) {
        self.remove_thinking_indicator_only();
        let observer = self.observer.borrow().clone();
        if let Some(observer) = observer {
            observer.thinking_removed();
        }
    }

    // ---------------------------------------------------------- streaming

    /// `_stream_begin`.
    fn stream_begin(self: &Rc<Self>) {
        self.remove_thinking_indicator();
        if self.last_bubble_role.borrow().as_deref() == Some("assistant")
            && let Some(previous) = self.last_bubble_box.borrow().as_ref()
        {
            previous.remove_css_class("last-in-group");
        }
        let row = gtk::ListBoxRow::new();
        row.set_selectable(false);
        row.add_css_class("message-row");
        row.add_css_class("assistant-message-row");
        let message_box = gtk::Box::new(gtk::Orientation::Horizontal, 12);
        message_box.add_css_class("message-box");
        message_box.add_css_class("assistant-message-box");
        message_box.add_css_class("last-in-group");
        message_box.set_halign(gtk::Align::Fill);
        message_box.set_hexpand(true);
        self.last_bubble_role.replace(Some("assistant".into()));
        self.last_bubble_box.replace(Some(message_box.clone()));
        message_box.append(&self.avatar());
        let bubble = gtk::Box::new(gtk::Orientation::Vertical, 6);
        bubble.add_css_class("message-bubble");
        bubble.add_css_class("assistant-bubble");
        bubble.set_halign(gtk::Align::Fill);
        bubble.set_hexpand(true);
        let content_label = gtk::Label::new(None);
        content_label.add_css_class("message-label");
        content_label.set_halign(gtk::Align::Fill);
        content_label.set_hexpand(true);
        content_label.set_wrap(true);
        content_label.set_selectable(true);
        content_label.set_xalign(0.0);
        bubble.append(&content_label);
        message_box.append(&bubble);
        row.set_child(Some(&message_box));
        self.chat_listbox.append(&row);

        let ctx = Rc::new(StreamCtx {
            row,
            bubble,
            content_label,
            reasoning: RefCell::default(),
            raw_content: RefCell::default(),
            reasoning_field: RefCell::default(),
            content_target: RefCell::default(),
            reasoning_target: RefCell::default(),
            content_shown: Cell::new(0),
            reasoning_shown: Cell::new(0),
            done: Cell::new(false),
            reply: RefCell::default(),
            speak: Cell::new(false),
            final_: Cell::new(true),
            finished: Cell::new(false),
        });
        self.stream.replace(Some(ctx.clone()));
        let weak = Rc::downgrade(self);
        let tick =
            glib::timeout_add_local(Duration::from_millis(16), move || match weak.upgrade() {
                Some(alexy) => {
                    if alexy.stream_tick(&ctx) {
                        glib::ControlFlow::Continue
                    } else {
                        glib::ControlFlow::Break
                    }
                }
                None => glib::ControlFlow::Break,
            });
        self.stream_tick.replace(Some(tick));
        let weak = Rc::downgrade(self);
        glib::timeout_add_local_once(Duration::from_millis(60), move || {
            if let Some(alexy) = weak.upgrade() {
                alexy.scroll_chat_to_bottom();
            }
        });
    }

    fn ensure_reasoning_expander(&self, ctx: &StreamCtx) {
        if ctx.reasoning.borrow().is_some() {
            return;
        }
        let expander = gtk::Expander::new(None);
        expander.set_expanded(false);
        expander.add_css_class("reasoning-expander");
        expander.set_halign(gtk::Align::Fill);
        expander.set_hexpand(true);
        let header = gtk::Box::new(gtk::Orientation::Horizontal, 6);
        header.set_halign(gtk::Align::Fill);
        header.set_hexpand(true);
        let spinner = gtk::Spinner::new();
        spinner.start();
        header.append(&spinner);
        let title = gtk::Label::new(Some(&tr("Thinking...")));
        title.add_css_class("dim-label");
        title.add_css_class("caption");
        title.set_halign(gtk::Align::Start);
        title.set_hexpand(true);
        header.append(&title);
        expander.set_label_widget(Some(&header));
        let label = gtk::Label::new(None);
        label.add_css_class("reasoning-text");
        label.add_css_class("dim-label");
        label.set_halign(gtk::Align::Fill);
        label.set_hexpand(true);
        label.set_wrap(true);
        label.set_selectable(true);
        label.set_xalign(0.0);
        label.set_margin_top(6);
        label.set_margin_start(6);
        label.set_margin_bottom(6);
        let scroll = gtk::ScrolledWindow::new();
        scroll.set_policy(gtk::PolicyType::Never, gtk::PolicyType::Automatic);
        scroll.set_max_content_height(220);
        scroll.set_propagate_natural_height(true);
        scroll.set_halign(gtk::Align::Fill);
        scroll.set_hexpand(true);
        scroll.set_child(Some(&label));
        expander.set_child(Some(&scroll));
        ctx.bubble.prepend(&expander);
        ctx.reasoning.replace(Some(Reasoning {
            label,
            spinner,
            title,
        }));
    }

    /// `_recompute_stream_targets`: inline `<think>` goes to the reasoning.
    fn recompute_stream_targets(&self, ctx: &StreamCtx) {
        static THINK: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"(<think>|</think>)").expect("valid"));
        let raw = ctx.raw_content.borrow().clone();
        let mut inline = String::new();
        let mut visible = String::new();
        let mut inside = false;
        let mut last = 0;
        let mut parts: Vec<&str> = Vec::new();
        for found in THINK.find_iter(&raw) {
            parts.push(&raw[last..found.start()]);
            parts.push(found.as_str());
            last = found.end();
        }
        parts.push(&raw[last..]);
        for part in parts {
            match part {
                "<think>" => inside = true,
                "</think>" => inside = false,
                part if inside => inline.push_str(part),
                part => visible.push_str(part),
            }
        }
        let mut reasoning = ctx.reasoning_field.borrow().clone();
        reasoning.push_str(&inline);
        ctx.content_target.replace(visible.chars().collect());
        let has_reasoning = !python_strip(&reasoning).is_empty();
        ctx.reasoning_target.replace(reasoning.chars().collect());
        if has_reasoning {
            self.ensure_reasoning_expander(ctx);
        }
    }

    fn stream_push(&self, content: &str, reasoning: &str) {
        let Some(ctx) = self.stream.borrow().clone() else {
            return;
        };
        ctx.raw_content.borrow_mut().push_str(content);
        ctx.reasoning_field.borrow_mut().push_str(reasoning);
        self.recompute_stream_targets(&ctx);
    }

    /// `_stream_tick`: a few more characters each frame; `false` once done.
    fn stream_tick(self: &Rc<Self>, ctx: &Rc<StreamCtx>) -> bool {
        let mut advanced = false;
        let r_target = ctx.reasoning_target.borrow().clone();
        if ctx.reasoning_shown.get() < r_target.len() {
            let remaining = r_target.len() - ctx.reasoning_shown.get();
            let step = (remaining / 8).max(3);
            ctx.reasoning_shown
                .set((ctx.reasoning_shown.get() + step).min(r_target.len()));
            if let Some(reasoning) = ctx.reasoning.borrow().as_ref() {
                reasoning.label.set_text(
                    &r_target[..ctx.reasoning_shown.get()]
                        .iter()
                        .collect::<String>(),
                );
            }
            advanced = true;
        }
        let c_target = ctx.content_target.borrow().clone();
        if ctx.content_shown.get() < c_target.len() {
            let remaining = c_target.len() - ctx.content_shown.get();
            let step = (remaining / 10).max(2);
            ctx.content_shown
                .set((ctx.content_shown.get() + step).min(c_target.len()));
            ctx.content_label.set_text(
                &c_target[..ctx.content_shown.get()]
                    .iter()
                    .collect::<String>(),
            );
            advanced = true;
        }
        if advanced {
            self.scroll_chat_to_bottom();
        }
        let caught_up = ctx.content_shown.get() >= c_target.len()
            && ctx.reasoning_shown.get() >= r_target.len();
        if ctx.done.get() && caught_up {
            self.stream_complete(ctx);
            return false;
        }
        true
    }

    fn stream_finish(self: &Rc<Self>, reply: String, speak: bool, final_: bool) {
        let ctx = self.stream.borrow().clone();
        let Some(ctx) = ctx else {
            if final_ {
                self.on_api_success(&reply);
            }
            return;
        };
        ctx.reply.replace(reply);
        ctx.speak.set(speak);
        ctx.final_.set(final_);
        ctx.done.set(true);
    }

    /// `_stream_complete`: markup applied, input unlocked, speech started.
    fn stream_complete(self: &Rc<Self>, ctx: &Rc<StreamCtx>) {
        if ctx.finished.replace(true) {
            return;
        }
        let reply = ctx.reply.borrow().clone();
        if !python_strip(&reply).is_empty() {
            ctx.content_label.set_markup(&markdown::to_pango(&reply));
        } else {
            ctx.content_label.set_visible(false);
        }
        if let Some(reasoning) = ctx.reasoning.borrow().as_ref() {
            reasoning.spinner.stop();
            reasoning.spinner.set_visible(false);
            reasoning.title.set_text(&tr("Reasoning"));
        }
        if self
            .stream
            .borrow()
            .as_ref()
            .is_some_and(|current| Rc::ptr_eq(current, ctx))
        {
            self.stream.replace(None);
        }
        if !ctx.final_.get() {
            return;
        }
        crate::screen::cleanup_tmp();
        self.save_and_unlock(ctx.speak.get(), &reply);
    }

    fn save_and_unlock(self: &Rc<Self>, speak: bool, reply: &str) {
        if speak {
            self.shared.speak_next.store(false, Ordering::SeqCst);
            self.llm_processing.set(false);
            self.spinner.stop();
            self.spinner.set_visible(false);
            self.save_conversation();
            self.play_tts(reply, None);
        } else {
            self.llm_processing.set(false);
            self.unlock_input();
            self.spinner.stop();
            self.spinner.set_visible(false);
            self.entry.grab_focus();
            self.save_conversation();
        }
    }

    fn unlock_input(&self) {
        self.entry.set_sensitive(true);
        self.send_btn.set_icon_name(&self.icon_send);
        self.stt_toggle.set_sensitive(true);
        self.new_conv_btn.set_sensitive(true);
        self.conv_toggle_btn.set_sensitive(true);
        self.settings_btn.set_sensitive(true);
    }

    fn stream_discard(&self) {
        let Some(ctx) = self.stream.take() else {
            return;
        };
        if let Some(tick) = self.stream_tick.take() {
            tick.remove();
        }
        self.chat_listbox.remove(&ctx.row);
        self.last_bubble_role.replace(None);
        self.last_bubble_box.replace(None);
    }

    // ------------------------------------------------------------- sending

    /// `on_send_clicked`.
    pub fn on_send_clicked(self: &Rc<Self>) {
        if self.llm_processing.get() || self.tts_playing.get() {
            self.cancel_generation();
            return;
        }
        let text = python_strip(&self.entry.text()).to_owned();
        if text.is_empty() && self.pending_images.borrow().is_empty() {
            return;
        }
        if self.stt_toggle.is_active() {
            self.stt_toggle.set_active(false);
        }
        let is_voice = self.last_input_was_voice.replace(false);
        self.shared.speak_next.store(is_voice, Ordering::SeqCst);
        let backend = self.config.borrow().backend.clone();
        if backend == "direct" && self.config.borrow().api_key.is_empty() {
            self.add_message_bubble(
                "assistant",
                &json!(tr("Please configure your API Key in settings first.")),
            );
            return;
        }
        let mut images: Vec<(String, String)> = self.pending_images.take();
        self.rebuild_image_preview();
        let screen_active = self.screen_awareness_active.get() || self.voice_autostart.get();
        println!(
            "[Screen Awareness] on_send_clicked: screen_awareness_active={}, _voice_autostart={}, effective={}",
            python_bool(self.screen_awareness_active.get()),
            python_bool(self.voice_autostart.get()),
            python_bool(screen_active)
        );
        let mut has_screen_capture = false;
        if screen_active {
            match crate::screen::capture_screenshot() {
                Some(screenshot) => {
                    images.push(screenshot);
                    has_screen_capture = true;
                }
                None => self.add_message_bubble(
                    "assistant",
                    &json!(tr("Failed to capture screenshot. No screenshot tool found (grim, gnome-screenshot, spectacle, scrot, or import).")),
                ),
            }
        }
        self.entry.set_text("");
        self.entry.set_sensitive(false);
        self.send_btn.set_icon_name(&self.icon_stop);
        self.stt_toggle.set_sensitive(false);
        self.new_conv_btn.set_sensitive(false);
        self.conv_toggle_btn.set_sensitive(false);
        self.settings_btn.set_sensitive(false);
        self.llm_processing.set(true);
        self.shared.abort.store(false, Ordering::SeqCst);
        self.spinner.set_visible(true);
        self.spinner.start();

        let correction = backend == "direct" && self.config.borrow().voice_correction_direct;
        if is_voice && correction {
            let config = self.config.borrow().clone();
            let shared = self.shared.clone();
            let weak = Rc::downgrade(self);
            glib::spawn_future_local(async move {
                let corrected = gio::spawn_blocking(move || {
                    crate::ai::correct_voice_text(
                        &text,
                        &config.api_url,
                        &config.api_key,
                        &config.model,
                    )
                })
                .await
                .unwrap_or_default();
                if shared.abort.load(Ordering::SeqCst) {
                    return;
                }
                if let Some(alexy) = weak.upgrade() {
                    guard("send", || {
                        alexy.proceed_with_message(&corrected, images, has_screen_capture)
                    });
                }
            });
        } else {
            self.proceed_with_message(&text, images, has_screen_capture);
        }
    }

    /// `_proceed_with_message`.
    fn proceed_with_message(
        self: &Rc<Self>,
        text: &str,
        images: Vec<(String, String)>,
        screen: bool,
    ) {
        if !images.is_empty() {
            let mut content = Vec::new();
            let mut display = Vec::new();
            if !text.is_empty() {
                let sent = if screen {
                    format!("{}{text}", conversations::SCREEN_AWARENESS_PREFIX)
                } else {
                    text.to_owned()
                };
                content.push(json!({"type": "text", "text": sent}));
                display.push(json!({"type": "text", "text": text}));
            }
            for (mime, data) in &images {
                let item = json!({"type": "image_url", "image_url": {"url": format!("data:{mime};base64,{data}")}});
                content.push(item.clone());
                display.push(item);
            }
            self.add_message_bubble("user", &Value::Array(display));
            if let Ok(mut history) = self.shared.history.lock() {
                history.push(json!({"role": "user", "content": content}));
            }
        } else {
            self.add_message_bubble("user", &json!(text));
            if let Ok(mut history) = self.shared.history.lock() {
                history.push(json!({"role": "user", "content": text}));
            }
        }
        self.show_thinking_indicator();
        self.start_turn();
    }

    /// Run `call_ai` on a worker thread and apply what it reports.
    pub fn start_turn(self: &Rc<Self>) {
        let config = self.config.borrow().clone();
        let settings = Settings {
            backend: config.backend,
            api_url: config.api_url,
            api_key: config.api_key,
            model: config.model,
            local_model: config.local_model,
            local_url: config.local_url,
            endpoint_url: config.endpoint_url,
            endpoint_model: config.endpoint_model,
            auto_execute_commands: config.auto_execute_commands,
        };
        let (sender, mut events) = futures_channel::mpsc::unbounded::<ToUi>();
        let mut turn = Turn {
            settings,
            shared: self.shared.clone(),
            host: self.host,
            ui: sender,
            pkexec: false,
        };
        let spawned = std::thread::Builder::new()
            .name("alexy-ai".into())
            .spawn(move || {
                if catch_unwind(AssertUnwindSafe(|| turn.call_ai())).is_err() {
                    let _ = turn
                        .ui
                        .unbounded_send(ToUi::ApiError("Error: the request failed".into()));
                }
            });
        if let Err(error) = spawned {
            self.on_api_error(&format!("Error: {error}"));
            return;
        }
        let weak = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            while let Some(event) = events.next().await {
                let Some(alexy) = weak.upgrade() else {
                    break;
                };
                guard("event", || alexy.handle(event));
            }
        });
    }

    fn handle(self: &Rc<Self>, event: ToUi) {
        match event {
            ToUi::StreamBegin => self.stream_begin(),
            ToUi::StreamPush(content, reasoning) => self.stream_push(&content, &reasoning),
            ToUi::StreamFinish {
                reply,
                speak,
                final_,
            } => self.stream_finish(reply, speak, final_),
            ToUi::StreamDiscard => self.stream_discard(),
            ToUi::ApiError(message) => self.on_api_error(&message),
            ToUi::Bubble(text) => self.add_message_bubble("assistant", &json!(text)),
            ToUi::PullModel(model) => {
                let weak = Rc::downgrade(self);
                self.on_pull_ollama_clicked(
                    &model,
                    Some(Box::new(move |success| {
                        if let Some(alexy) = weak.upgrade() {
                            if success {
                                alexy.start_turn();
                            } else {
                                alexy.on_api_error(&tr("Model download was cancelled or failed."));
                            }
                        }
                    })),
                    None,
                );
            }
            ToUi::InstallOllama => {
                let weak = Rc::downgrade(self);
                self.on_ollama_install_clicked(Some(Box::new(move |success| {
                    let Some(alexy) = weak.upgrade() else {
                        return;
                    };
                    if !success {
                        alexy.on_api_error(&tr("Ollama installation was cancelled or failed."));
                        return;
                    }
                    let _ = std::process::Command::new("systemctl")
                        .args(["enable", "--now", "ollama"])
                        .output();
                    if alexy.config.borrow().local_model.is_empty() {
                        alexy.on_api_error(&tr(
                            "Ollama Installed! Please open Settings to pull an AI model.",
                        ));
                    } else {
                        alexy.start_turn();
                    }
                })));
            }
            ToUi::EndpointModel(model) => {
                self.config.borrow_mut().endpoint_model = model;
                self.update_subtitle();
            }
            ToUi::AskSafety { code, reply } => self.ask_safety(&code, reply),
            ToUi::AskPassword { message, reply } => {
                let yes = reply.clone();
                self.prompt_for_password_dialog(
                    Box::new(move || {
                        let _ = yes.send(true);
                    }),
                    &message,
                    Some(Box::new(move || {
                        let _ = reply.send(false);
                    })),
                );
            }
        }
    }

    fn ask_safety(&self, code: &str, reply: std::sync::mpsc::Sender<bool>) {
        let message = tr("The assistant wants to run the following command:\n\n{0}\n\nDo you want to allow this?")
            .replacen("{0}", code, 1);
        let dialog = adw::MessageDialog::new(
            self.window_or_root().as_ref(),
            Some(&tr("Command Execution Request")),
            Some(&message),
        );
        dialog.add_response("deny", &tr("Deny"));
        dialog.add_response("allow", &tr("Allow"));
        dialog.set_response_appearance("allow", adw::ResponseAppearance::Destructive);
        dialog.connect_response(None, move |_, response| {
            let _ = reply.send(response == "allow");
        });
        self.translate(&dialog);
        dialog.present();
    }

    /// `_prompt_for_password_dialog`.
    pub fn prompt_for_password_dialog(
        self: &Rc<Self>,
        success: Box<dyn FnOnce()>,
        message: &str,
        cancel: Option<Box<dyn FnOnce()>>,
    ) {
        let Some(host) = self.host else {
            self.add_message_bubble(
                "assistant",
                &json!(tr("Sudo manager not available in environment.")),
            );
            if let Some(cancel) = cancel {
                cancel();
            }
            return;
        };
        let dialog = adw::MessageDialog::new(
            self.window_or_root().as_ref(),
            Some(&tr("Authentication Required")),
            Some(message),
        );
        dialog.add_response("cancel", &tr("Cancel"));
        dialog.add_response("authenticate", &tr("Authenticate"));
        let content = gtk::Box::new(gtk::Orientation::Vertical, 12);
        let entry = gtk::PasswordEntry::new();
        content.append(&entry);
        dialog.set_extra_child(Some(&content));
        let callbacks = RefCell::new(Some((success, cancel)));
        let weak = Rc::downgrade(self);
        dialog.connect_response(None, move |_, response| {
            let Some((success, cancel)) = callbacks.borrow_mut().take() else {
                return;
            };
            if response != "authenticate" {
                if let Some(cancel) = cancel {
                    cancel();
                }
                return;
            }
            let password = entry.text().to_string();
            let weak = weak.clone();
            glib::spawn_future_local(async move {
                let checked = password.clone();
                let valid = !checked.is_empty()
                    && gio::spawn_blocking(move || host.sudo_validate_password(&checked))
                        .await
                        .unwrap_or(false);
                if valid {
                    host.sudo_set_password(&password);
                    success();
                    return;
                }
                let Some(alexy) = weak.upgrade() else {
                    if let Some(cancel) = cancel {
                        cancel();
                    }
                    return;
                };
                let error = adw::MessageDialog::new(
                    alexy.window_or_root().as_ref(),
                    Some(&tr("Error")),
                    Some(&tr("Invalid password.")),
                );
                error.add_response("ok", &tr("OK"));
                let cancel = RefCell::new(cancel);
                error.connect_response(None, move |_, _| {
                    if let Some(cancel) = cancel.borrow_mut().take() {
                        cancel();
                    }
                });
                alexy.translate(&error);
                error.present();
            });
        });
        self.translate(&dialog);
        dialog.present();
    }

    /// `cancel_generation`.
    pub fn cancel_generation(self: &Rc<Self>) {
        if self.tts_playing.get() && !self.llm_processing.get() {
            self.stop_tts();
            return;
        }
        self.stop_tts();
        self.shared.abort.store(true, Ordering::SeqCst);
        self.llm_processing.set(false);
        self.stream_discard();
        self.spinner.stop();
        self.spinner.set_visible(false);
        self.unlock_input();
        self.add_message_bubble("assistant", &json!(tr("Generation stopped by user.")));
    }

    /// `on_api_success` (a reply that was not streamed).
    fn on_api_success(self: &Rc<Self>, reply: &str) {
        if self.shared.abort.load(Ordering::SeqCst) {
            return;
        }
        self.remove_thinking_indicator();
        crate::screen::cleanup_tmp();
        if self.shared.speak_next.load(Ordering::SeqCst) {
            self.shared.speak_next.store(false, Ordering::SeqCst);
            self.add_message_bubble("assistant", &json!(reply));
            self.llm_processing.set(false);
            self.spinner.stop();
            self.spinner.set_visible(false);
            self.save_conversation();
            self.play_tts(reply, None);
        } else {
            self.llm_processing.set(false);
            self.unlock_input();
            self.spinner.stop();
            self.spinner.set_visible(false);
            self.entry.grab_focus();
            self.save_conversation();
            self.add_message_bubble("assistant", &json!(reply));
        }
    }

    /// `on_api_error`.
    pub fn on_api_error(self: &Rc<Self>, message: &str) {
        if self.shared.abort.load(Ordering::SeqCst) {
            return;
        }
        self.remove_thinking_indicator();
        crate::screen::cleanup_tmp();
        self.llm_processing.set(false);
        self.add_message_bubble(
            "assistant",
            &json!(format!("{}{message}", tr("⚠️ Error: "))),
        );
        if let Ok(mut history) = self.shared.history.lock()
            && history.len() > 1
        {
            history.pop();
        }
        self.unlock_input();
        self.spinner.stop();
        self.spinner.set_visible(false);
        self.entry.grab_focus();
    }

    // ------------------------------------------------------------ images

    fn on_screen_toggle(&self, active: bool) {
        println!(
            "[Screen Awareness] _on_screen_toggle called: active={}",
            python_bool(active)
        );
        self.screen_awareness_active.set(active);
        if active {
            self.screen_toggle.add_css_class("suggested-action");
        } else {
            self.screen_toggle.remove_css_class("suggested-action");
        }
    }

    fn add_image_from_texture(self: &Rc<Self>, texture: &gdk::Texture) {
        let png = texture.save_to_png_bytes();
        let data = base64::engine::general_purpose::STANDARD.encode(&png);
        self.pending_images
            .borrow_mut()
            .push(("image/png".into(), data));
        self.rebuild_image_preview();
    }

    fn add_image_from_file(self: &Rc<Self>, path: &std::path::Path) {
        match std::fs::read(path) {
            Ok(raw) => {
                if gdk::Texture::from_bytes(&glib::Bytes::from(&raw)).is_err() {
                    println!(
                        "[Image] Failed to load file {}: not an image",
                        path.display()
                    );
                    return;
                }
                let data = base64::engine::general_purpose::STANDARD.encode(&raw);
                self.pending_images
                    .borrow_mut()
                    .push((guess_mime_type(path).into(), data));
                self.rebuild_image_preview();
            }
            Err(error) => println!("[Image] Failed to load file {}: {error}", path.display()),
        }
    }

    /// `_rebuild_image_preview`.
    pub fn rebuild_image_preview(self: &Rc<Self>) {
        while let Some(child) = self.image_preview_box.first_child() {
            self.image_preview_box.remove(&child);
        }
        let images = self.pending_images.borrow().clone();
        for (index, (_, data)) in images.iter().enumerate() {
            let Ok(raw) = base64::engine::general_purpose::STANDARD.decode(data) else {
                continue;
            };
            let Ok(texture) = gdk::Texture::from_bytes(&glib::Bytes::from_owned(raw)) else {
                continue;
            };
            let overlay = gtk::Overlay::new();
            let picture = gtk::Picture::for_paintable(&texture);
            picture.set_size_request(60, 60);
            picture.set_can_shrink(true);
            picture.set_content_fit(gtk::ContentFit::Cover);
            let frame = gtk::Frame::new(None);
            frame.set_child(Some(&picture));
            frame.set_size_request(60, 60);
            overlay.set_child(Some(&frame));
            let close = gtk::Button::new();
            set_button_icon(
                &close,
                &["window-close-symbolic", "window-close", "dialog-close"],
                Some("✕"),
            );
            close.add_css_class("circular");
            close.add_css_class("osd");
            close.set_halign(gtk::Align::End);
            close.set_valign(gtk::Align::Start);
            close.set_margin_top(2);
            close.set_margin_end(2);
            let weak = Rc::downgrade(self);
            close.connect_clicked(move |_| {
                if let Some(alexy) = weak.upgrade() {
                    let removed = {
                        let mut images = alexy.pending_images.borrow_mut();
                        (index < images.len()).then(|| images.remove(index))
                    };
                    if removed.is_some() {
                        alexy.rebuild_image_preview();
                    }
                }
            });
            overlay.add_overlay(&close);
            self.image_preview_box.append(&overlay);
        }
        self.image_preview_box
            .set_visible(!self.pending_images.borrow().is_empty());
    }
}

/// Python's `True`/`False` in printed lines.
pub fn python_bool(value: bool) -> &'static str {
    if value { "True" } else { "False" }
}

/// `_is_image_file`.
fn is_image_file(path: &std::path::Path) -> bool {
    let extension = path
        .extension()
        .map(|extension| format!(".{}", extension.to_string_lossy().to_lowercase()))
        .unwrap_or_default();
    [
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp",
    ]
    .contains(&extension.as_str())
}

/// `_guess_mime_type`.
pub(crate) fn guess_mime_type(path: &std::path::Path) -> &'static str {
    let extension = path
        .extension()
        .map(|extension| extension.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    match extension.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "bmp" => "image/bmp",
        "tiff" | "tif" => "image/tiff",
        "webp" => "image/webp",
        _ => "image/png",
    }
}
