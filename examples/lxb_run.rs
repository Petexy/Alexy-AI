//! Plays the LineXinBar page headless, as the Center's LineXinBar front end
//! would show it alone (`-w`): loads the built widget library through the C
//! ABI, draws its page for a number of frames with `lxb_app_shot_after`
//! (each frame waits a little, so the page's threads run in real time), hands
//! it actions on the frames named (through the window, so menus and questions
//! take them), and writes the last frame to a PNG. What is typed is scripted
//! with `LINEXIN_LXB_TYPED` (see `linexin_lxb::TYPED_VARIABLE`).
//!
//! ```text
//! lxb_run --library LIB.so --out PNG [--frames N] [--voice] [FRAME:ACTION]...
//! ```
//!
//! ACTION is an lxb action name (`accept`, `down`, `back`, ...). Needs a
//! liblxb_app.so with `lxb_app_shot_after`; examples/compare.sh runs it in
//! its fake environment and checks what the page ran and asked.

use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::OnceLock;
use std::time::Duration;

use linexin_localization::Localization;
use linexin_lxb::{Action, App, Overlay, Panes, host_frame, raw_actions};
use linexin_plugin_api::{
    ABI_VERSION, ENTRY_SYMBOL, EntryV1, Header, HostApiV1, LxbContextV1, WidgetDescriptorV1,
};

const MANIFEST_DIR: &str = env!("CARGO_MANIFEST_DIR");
/// How long each frame waits: 600 frames are about seven seconds.
const FRAME_PAUSE: Duration = Duration::from_millis(12);

static LANGUAGE: OnceLock<CString> = OnceLock::new();

unsafe extern "C" fn language() -> *const c_char {
    LANGUAGE
        .get()
        .map_or(c"en_US".as_ptr(), |language| language.as_ptr())
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

unsafe extern "C" fn no_widget(_: *const c_char, _: *mut c_void) {}
unsafe extern "C" fn no_op() {}
unsafe extern "C" fn refuse(_: *const c_char) -> bool {
    false
}
unsafe extern "C" fn ignore(_: *const c_char) {}
unsafe extern "C" fn no() -> bool {
    false
}
unsafe extern "C" fn no_path() -> *const c_char {
    c"/bin/false".as_ptr()
}
unsafe extern "C" fn refused(_: *const *const c_char) -> c_int {
    1
}
unsafe extern "C" fn not_compact(_: *mut c_void) -> bool {
    false
}

/// The Center's LineXinBar host: translations, and no sudo password ever.
static HOST: HostApiV1 = HostApiV1 {
    abi_version: ABI_VERSION,
    struct_size: size_of::<HostApiV1>() as u32,
    language,
    translate,
    free_string,
    translate_widget: no_widget,
    lock_ui: no_op,
    unlock_ui: no_op,
    sudo_validate_password: refuse,
    sudo_set_password: ignore,
    sudo_has_password: no,
    sudo_forget_password: no_op,
    sudo_wrapper_path: no_path,
    sudo_start_session: no_op,
    sudo_stop_session: no_op,
    sudo_run_privileged: refused,
    refresh_widgets: no_op,
    sudo_askpass_path: no_path,
    window_compact_mode: not_compact,
};

fn main() -> ExitCode {
    // Beside the examples/ holding this one, wherever cargo built them.
    let mut library = std::env::current_exe()
        .ok()
        .and_then(|run| Some(run.parent()?.parent()?.join("libalexy_ai.so")))
        .unwrap_or_else(|| PathBuf::from("libalexy_ai.so"));
    let mut out = PathBuf::from("lxb_run.png");
    let mut frames = 600u32;
    let mut voice = false;
    let mut steps: Vec<(u32, Action)> = Vec::new();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--library" => library = args.next().unwrap_or_default().into(),
            "--out" => out = args.next().unwrap_or_default().into(),
            "--frames" => frames = args.next().and_then(|n| n.parse().ok()).unwrap_or(frames),
            "--voice" => voice = true,
            step => {
                let parsed = step.split_once(':').and_then(|(frame, name)| {
                    let action = Action::ALL
                        .iter()
                        .copied()
                        .find(|action| action.key() == name)?;
                    Some((frame.parse().ok()?, action))
                });
                match parsed {
                    Some(step) => steps.push(step),
                    None => {
                        eprintln!("lxb_run: bad step {step:?}");
                        return ExitCode::FAILURE;
                    }
                }
            }
        }
    }
    let _ = LANGUAGE.set(
        CString::new(std::env::var("LANGUAGE_CODE").unwrap_or_else(|_| "en_US".into()))
            .unwrap_or_default(),
    );
    linexin_localization::install(Localization::load(
        &[PathBuf::from(MANIFEST_DIR).join("data/usr/share/linexin/widgets/localization")],
        &LANGUAGE
            .get()
            .map(|l| l.to_string_lossy().into_owned())
            .unwrap_or_default(),
        None,
    ));
    match run(&library, &out, frames, voice, &steps) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("lxb_run: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run(
    library: &Path,
    out: &Path,
    frames: u32,
    voice: bool,
    steps: &[(u32, Action)],
) -> Result<(), String> {
    // SAFETY: a trusted development build of the widget.
    let library = unsafe { libloading::Library::new(library) }.map_err(|e| e.to_string())?;
    let library = Box::leak(Box::new(library));
    let entry = unsafe { library.get::<EntryV1>(ENTRY_SYMBOL) }.map_err(|e| e.to_string())?;
    let descriptor = unsafe { entry(&HOST) };
    if !unsafe { Header::is_compatible(descriptor.cast(), size_of::<WidgetDescriptorV1>()) } {
        return Err("incompatible descriptor".into());
    }
    let descriptor = unsafe { &*descriptor };
    let (create, page_fn, destroy) = match (
        descriptor.lxb_create,
        descriptor.lxb_page,
        descriptor.lxb_destroy,
    ) {
        (Some(create), Some(page), Some(destroy)) => (create, page, destroy),
        _ => return Err("the widget has no LineXinBar page".into()),
    };
    let context = LxbContextV1 {
        abi_version: ABI_VERSION,
        struct_size: size_of::<LxbContextV1>() as u32,
        host: &HOST,
        hide_sidebar: true,
        voice_autostart: voice,
        conversation_id: std::ptr::null(),
    };
    let state = unsafe { create(&context) };
    if state.is_null() {
        return Err("lxb_create returned null".into());
    }
    let mut app = App::new("com.linexin.AlexyRun", "Alexy AI")?
        .plain()
        .driven();
    let mut frame = 0u32;
    let result = app.shot_after(out, 1600, 900, 0.5, frames, |page| {
        // Pressed through the window, as a key would be: a menu or a question
        // standing over the page takes it, and otherwise the page reads it,
        // as the Center hands it on.
        for (_, action) in steps.iter().filter(|(at, _)| *at == frame) {
            page.act(*action);
        }
        let actions = page.actions();
        frame += 1;
        let panes = Panes::whole(page);
        page.draw_pane(panes.window, Overlay::Dialog);
        let raw = raw_actions(&actions);
        let host = host_frame(page, panes.content, true, &raw);
        // SAFETY: the state from lxb_create; the frame lives for the call.
        unsafe { page_fn(state, &host) };
        std::thread::sleep(FRAME_PAUSE);
    });
    unsafe { destroy(state) };
    println!("lxb_run: {frame} frames drawn");
    result
}
