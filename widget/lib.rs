//! Native port of the Alexy AI widget (`aa-alexy-ai-widget.py`), installed as
//! `/usr/share/linexin/widgets/aa-alexy-ai-widget.so`.
//!
//! The GTK page (`ui`, `settings`, `voice`, `tts`, `screen`) is the Python
//! widget's; `compact` is its `--compact` voice window; `lxb` the LineXinBar
//! page. Models are reached through `ai` and `worker`.

mod ai;
mod compact;
mod config;
mod conversations;
mod entry;
mod lxb;
mod markdown;
mod progress;
mod pyjson;
mod pyrepr;
mod screen;
mod settings;
mod theme;
mod tts;
mod ui;
mod voice;
mod worker;

use std::ffi::c_void;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::PathBuf;
use std::ptr;
use std::rc::Rc;

use gtk::glib::translate::{FromGlibPtrBorrow, FromGlibPtrNone, IntoGlibPtr};
use gtk::prelude::*;
use linexin_plugin_api as api;

static WIDGET: api::WidgetDescriptorV1 = api::WidgetDescriptorV1::new(
    c"aa_alexy_ai",
    c"Alexy AI",
    c"/usr/share/icons/github.petexy.alexy.svg",
    Some(create),
)
.with_close_request(close_request)
.with_reactivate_voice(reactivate_voice)
.with_compact_voice(create_compact_voice, reactivate_compact_voice)
.with_lxb_page(c"setting-microphone", lxb::create, lxb::page, lxb::destroy)
.with_lxb_reactivate_voice(lxb::reactivate_voice);
api::export_widget!(WIDGET);

/// The state of a created widget, kept on its root.
const STATE_KEY: &str = "alexy-ai-state";
const COMPACT_KEY: &str = "alexy-ai-compact";

unsafe extern "C" fn create(context: *const api::CreateContextV1) -> *mut c_void {
    let widget = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: `context` is the pointer the host passed to `create`.
        let context = unsafe { api::CreateContextV1::read(context) }?;
        if let Err(error) = adw::init() {
            eprintln!("Alexy AI: could not initialize GTK: {error}");
            return None;
        }
        let window = (!context.window.is_null())
            .then(|| {
                // SAFETY: the host passes a live GtkWindow*; a new reference is taken.
                let object = unsafe {
                    gtk::glib::Object::from_glib_none(
                        context.window.cast::<gtk::glib::gobject_ffi::GObject>(),
                    )
                };
                object.downcast::<gtk::Window>().ok()
            })
            .flatten();
        let (root, alexy) = ui::build(
            api::host(),
            window,
            ui::Launch {
                voice_autostart: context.voice_autostart,
                conversation_id: context.conversation_id(),
            },
        );
        // SAFETY: only read back as Rc<ui::Alexy> under this key.
        unsafe { root.set_data(STATE_KEY, alexy) };
        let widget: *mut gtk::ffi::GtkWidget = root.upcast::<gtk::Widget>().into_glib_ptr();
        Some(widget.cast::<c_void>())
    }));
    match widget {
        Ok(Some(widget)) => widget,
        Ok(None) => ptr::null_mut(),
        Err(_) => {
            eprintln!("Alexy AI: widget creation panicked");
            ptr::null_mut()
        }
    }
}

/// The state kept on a widget the host hands back.
///
/// # Safety
/// `widget` must be a live widget returned by `create`.
unsafe fn state_of(widget: *mut c_void) -> Option<Rc<ui::Alexy>> {
    if widget.is_null() {
        return None;
    }
    // SAFETY: guaranteed by the caller.
    let widget = unsafe { gtk::Widget::from_glib_borrow(widget.cast::<gtk::ffi::GtkWidget>()) };
    // SAFETY: set in `create` with this type.
    unsafe { widget.data::<Rc<ui::Alexy>>(STATE_KEY) }
        .map(|state| unsafe { state.as_ref() }.clone())
}

/// The Center's window closes: the original's `_cleanup_screenshot_tmp`.
unsafe extern "C" fn close_request(_widget: *mut c_void) {
    let _ = catch_unwind(screen::cleanup_tmp);
}

unsafe extern "C" fn reactivate_voice(widget: *mut c_void) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: the host passes the widget returned by `create`.
        if let Some(alexy) = unsafe { state_of(widget) } {
            alexy.reactivate_voice();
        }
    }));
}

unsafe extern "C" fn create_compact_voice(
    context: *const api::CompactVoiceContextV1,
) -> *mut c_void {
    let window = catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: the pointer the host passed.
        let context = unsafe { api::CompactVoiceContextV1::read(context) }?;
        if let Err(error) = adw::init() {
            eprintln!("Alexy AI: could not initialize GTK: {error}");
            return None;
        }
        let application = (!context.application.is_null())
            .then(|| {
                // SAFETY: a live GtkApplication*; a new reference is taken.
                let object = unsafe {
                    gtk::glib::Object::from_glib_none(
                        context
                            .application
                            .cast::<gtk::glib::gobject_ffi::GObject>(),
                    )
                };
                object.downcast::<gtk::Application>().ok()
            })
            .flatten();
        let compact = compact::build(application, context.voice_autostart);
        let window = compact.window().clone();
        // SAFETY: only read back as Rc<compact::Compact> under this key.
        unsafe { window.set_data(COMPACT_KEY, compact) };
        let pointer: *mut gtk::ffi::GtkWindow = window.upcast::<gtk::Window>().into_glib_ptr();
        Some(pointer.cast::<c_void>())
    }));
    match window {
        Ok(Some(window)) => window,
        _ => ptr::null_mut(),
    }
}

unsafe extern "C" fn reactivate_compact_voice(window: *mut c_void) {
    let _ = catch_unwind(AssertUnwindSafe(|| {
        if window.is_null() {
            return;
        }
        // SAFETY: the window returned by create_compact_voice.
        let window = unsafe { gtk::Widget::from_glib_borrow(window.cast::<gtk::ffi::GtkWidget>()) };
        // SAFETY: set in create_compact_voice with this type.
        if let Some(compact) = unsafe { window.data::<Rc<compact::Compact>>(COMPACT_KEY) } {
            // SAFETY: alive while the window is.
            unsafe { compact.as_ref() }.reactivate();
        }
    }));
}

/// The folder the installed library is in (the Python widget's
/// `/usr/share/linexin/widgets`), where `themes/` and `sounds/` lie.
fn widgets_dir() -> PathBuf {
    let mut info: libc::Dl_info = unsafe { std::mem::zeroed() };
    // SAFETY: dladdr only reads the address of a function in this library.
    let found = unsafe { libc::dladdr(widgets_dir as *const c_void, &mut info) } != 0;
    let library = (found && !info.dli_fname.is_null()).then(|| {
        // SAFETY: dladdr filled a NUL-terminated path.
        PathBuf::from(
            unsafe { std::ffi::CStr::from_ptr(info.dli_fname) }
                .to_string_lossy()
                .into_owned(),
        )
    });
    library
        .and_then(|path| path.parent().map(PathBuf::from))
        .filter(|dir| dir.join("themes").is_dir())
        .unwrap_or_else(|| PathBuf::from("/usr/share/linexin/widgets"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CStr;

    #[test]
    fn descriptor_matches_the_python_widget() {
        let text = |pointer| unsafe { CStr::from_ptr(pointer) }.to_str().unwrap();
        assert_eq!(text(WIDGET.widget_id), ui::DOMAIN);
        assert_eq!(text(WIDGET.name), "Alexy AI");
        assert_eq!(
            text(WIDGET.icon),
            "/usr/share/icons/github.petexy.alexy.svg"
        );
        assert!(WIDGET.create.is_some() && WIDGET.lxb_page.is_some());
        assert!(WIDGET.reactivate_voice.is_some() && WIDGET.create_compact_voice.is_some());
    }

    #[test]
    fn entry_point_rejects_a_missing_host() {
        assert!(unsafe { linexin_widget_v1(ptr::null()) }.is_null());
        assert!(unsafe { create(ptr::null()) }.is_null());
    }
}
