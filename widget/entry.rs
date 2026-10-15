//! The original `MultilineEntry`: a text view in a scrolled window that looks
//! like an entry, grows with its text (40 to 140 pixels) and shows a
//! placeholder while empty.

use gtk::prelude::*;
use gtk::{gdk, glib, pango};
use std::cell::Cell;
use std::rc::Rc;

const CSS: &str = "
                scrolledwindow.multiline-entry {
                    min-height: 0px;
                    min-width: 0px;
                    background-color: @view_bg_color;
                    border: 1px solid @borders;
                    border-radius: 6px;
                    transition: outline 200ms cubic-bezier(0.25, 0.46, 0.45, 0.94);
                }
                scrolledwindow.multiline-entry scrollbar,
                scrolledwindow.multiline-entry scrollbar slider {
                    min-height: 0px;
                    min-width: 0px;
                }
                scrolledwindow.multiline-entry:focus-within {
                    outline: 2px solid @accent_bg_color;
                    outline-offset: -2px;
                }
                scrolledwindow.multiline-entry textview {
                    background-color: transparent;
                }
            ";

#[derive(Clone)]
pub struct MultilineEntry {
    pub scrolled: gtk::ScrolledWindow,
    pub textview: gtk::TextView,
    placeholder_label: gtk::Label,
    buffer: gtk::TextBuffer,
}

impl MultilineEntry {
    pub fn new() -> Self {
        thread_local! {
            static CSS_LOADED: Cell<bool> = const { Cell::new(false) };
        }
        if !CSS_LOADED.get()
            && let Some(display) = gdk::Display::default()
        {
            let provider = gtk::CssProvider::new();
            provider.load_from_string(CSS);
            gtk::style_context_add_provider_for_display(
                &display,
                &provider,
                gtk::STYLE_PROVIDER_PRIORITY_APPLICATION,
            );
            CSS_LOADED.set(true);
        }

        let scrolled = gtk::ScrolledWindow::new();
        scrolled.set_policy(gtk::PolicyType::Never, gtk::PolicyType::Automatic);
        scrolled.set_propagate_natural_height(false);
        scrolled.set_has_frame(false);
        scrolled.add_css_class("multiline-entry");

        let textview = gtk::TextView::new();
        textview.set_wrap_mode(gtk::WrapMode::WordChar);
        textview.set_left_margin(12);
        textview.set_right_margin(12);
        textview.set_top_margin(8);
        textview.set_bottom_margin(8);
        textview.set_valign(gtk::Align::Fill);

        let overlay = gtk::Overlay::new();
        overlay.set_child(Some(&textview));
        let placeholder_label = gtk::Label::new(None);
        placeholder_label.add_css_class("dim-label");
        placeholder_label.set_halign(gtk::Align::Start);
        placeholder_label.set_valign(gtk::Align::Start);
        placeholder_label.set_margin_start(12);
        placeholder_label.set_margin_top(8);
        placeholder_label.set_can_target(false);
        overlay.add_overlay(&placeholder_label);

        scrolled.set_child(Some(&overlay));
        scrolled.set_valign(gtk::Align::End);
        scrolled.set_size_request(-1, 40);

        let buffer = textview.buffer();
        let entry = Self {
            scrolled,
            textview,
            placeholder_label,
            buffer,
        };
        let weak_scrolled = entry.scrolled.downgrade();
        let weak_view = entry.textview.downgrade();
        let placeholder = entry.placeholder_label.downgrade();
        entry.buffer.connect_changed(move |buffer| {
            let has_text = buffer.char_count() > 0;
            if let Some(placeholder) = placeholder.upgrade() {
                placeholder.set_visible(!has_text);
            }
            let (scrolled, view) = (weak_scrolled.clone(), weak_view.clone());
            glib::idle_add_local_once(move || {
                let (Some(scrolled), Some(view)) = (scrolled.upgrade(), view.upgrade()) else {
                    return;
                };
                let buffer = view.buffer();
                let text = buffer.text(&buffer.start_iter(), &buffer.end_iter(), true);
                let layout = view.create_pango_layout(Some(&text));
                let width = view.width() - 24;
                if width > 0 {
                    layout.set_width(width * pango::SCALE);
                }
                layout.set_wrap(pango::WrapMode::WordChar);
                let (_, logical) = layout.pixel_extents();
                let total_height = logical.height() + 18;
                scrolled.set_size_request(-1, total_height.clamp(40, 140));
            });
        });
        entry
    }

    pub fn set_placeholder_text(&self, text: &str) {
        self.placeholder_label.set_label(text);
    }

    pub fn text(&self) -> String {
        self.buffer
            .text(&self.buffer.start_iter(), &self.buffer.end_iter(), true)
            .to_string()
    }

    pub fn set_text(&self, text: &str) {
        self.buffer.set_text(text);
    }

    /// Only the text view: the original overrode `set_sensitive` so.
    pub fn set_sensitive(&self, sensitive: bool) {
        self.textview.set_sensitive(sensitive);
    }

    pub fn grab_focus(&self) -> bool {
        self.textview.grab_focus()
    }

    /// Enter (without Shift) calls `callback`.
    pub fn connect_activate(&self, callback: impl Fn() + 'static) {
        let controller = gtk::EventControllerKey::new();
        let callback = Rc::new(callback);
        controller.connect_key_pressed(move |_, key, _, state| {
            if (key == gdk::Key::Return || key == gdk::Key::KP_Enter)
                && !state.contains(gdk::ModifierType::SHIFT_MASK)
            {
                callback();
                return glib::Propagation::Stop;
            }
            glib::Propagation::Proceed
        });
        self.textview.add_controller(controller);
    }
}
