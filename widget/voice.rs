//! Voice input: the original's `on_stt_toggled`, `_stt_start_whisper`,
//! `_begin_whisper_recording`, `_stt_start_vosk`, `_check_stt_availability`,
//! `_play_activation_sound` and the "Hey Alexy" daemon switch.
//!
//! Recording and silence detection are done here; Whisper and Vosk are Python
//! libraries, so a small Python helper process (`python3 -c`) loads the model
//! once and transcribes, as the original did in its own process.

use crate::ui::{Alexy, guard, tr};
use gtk::prelude::*;
use gtk::{gio, glib};
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// The Whisper helper: loads a model on request and transcribes WAV files.
const WHISPER_HELPER: &str = r#"
import json, sys
out = sys.stdout
sys.stdout = sys.stderr
model = None
for line in sys.stdin:
    request = json.loads(line)
    try:
        if request["op"] == "load":
            try:
                import whisper
            except ImportError:
                out.write(json.dumps({"error": "openai-whisper is not installed."}) + "\n")
                out.flush()
                continue
            model = whisper.load_model(request["model"])
            out.write(json.dumps({"ok": True}) + "\n")
        else:
            result = model.transcribe(request["path"], fp16=False)
            out.write(json.dumps({"text": result.get("text", "").strip(), "language": result.get("language", "")}) + "\n")
    except Exception as error:
        out.write(json.dumps({"error": str(error)}) + "\n")
    out.flush()
"#;

/// The Vosk helper: raw 16 kHz audio on stdin, one line per 4000-byte chunk
/// with what is recognized so far, the final text when the audio ends.
const VOSK_HELPER: &str = r#"
import json, sys
out = sys.stdout
sys.stdout = sys.stderr
try:
    import vosk
except ImportError:
    out.write(json.dumps({"missing": True}) + "\n")
    out.flush()
    sys.exit(0)
vosk.SetLogLevel(-1)
try:
    model = vosk.Model(sys.argv[1])
    recognizer = vosk.KaldiRecognizer(model, 16000)
except Exception as error:
    out.write(json.dumps({"error": str(error)}) + "\n")
    out.flush()
    sys.exit(0)
out.write(json.dumps({"ready": True}) + "\n")
out.flush()
stream = sys.stdin.buffer
while True:
    data = stream.read(4000)
    if not data:
        break
    text = ""
    if recognizer.AcceptWaveform(data):
        result = json.loads(recognizer.Result())
        if result.get("text"):
            text = result["text"]
    else:
        text = json.loads(recognizer.PartialResult()).get("partial", "")
    out.write(json.dumps({"text": text}) + "\n")
    out.flush()
try:
    final = json.loads(recognizer.FinalResult()).get("text", "")
except Exception:
    final = ""
out.write(json.dumps({"final": final}) + "\n")
out.flush()
"#;

struct Helper {
    child: Child,
    /// `None` once closed (the Vosk helper then gives its final text).
    input: Option<ChildStdin>,
    output: BufReader<ChildStdout>,
}

impl Helper {
    fn start(script: &str, args: &[&str]) -> std::io::Result<Self> {
        let mut child = Command::new("python3")
            .arg("-c")
            .arg(script)
            .args(args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()?;
        let input = child.stdin.take();
        let output = BufReader::new(child.stdout.take().expect("piped stdout"));
        Ok(Self {
            child,
            input,
            output,
        })
    }

    fn write(&mut self, data: &[u8]) -> std::io::Result<()> {
        let input = self
            .input
            .as_mut()
            .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::BrokenPipe))?;
        input.write_all(data)?;
        input.flush()
    }

    fn request(&mut self, request: &Value) -> Value {
        let sent = self.write(format!("{request}\n").as_bytes());
        if let Err(error) = sent {
            return json!({"error": error.to_string()});
        }
        self.read()
    }

    fn read(&mut self) -> Value {
        let mut line = String::new();
        match self.output.read_line(&mut line) {
            Ok(0) | Err(_) => json!({"error": "the voice helper stopped"}),
            Ok(_) => serde_json::from_str(&line)
                .unwrap_or_else(|error| json!({"error": error.to_string()})),
        }
    }
}

impl Drop for Helper {
    /// Closing its input ends the helper's loop, so Python exits on its own
    /// and its libraries clean up after themselves (killed outright,
    /// multiprocessing warns about the semaphores it left); one still busy
    /// after a moment is killed.
    fn drop(&mut self) {
        drop(self.input.take());
        let deadline = Instant::now() + std::time::Duration::from_secs(3);
        while Instant::now() < deadline {
            if !matches!(self.child.try_wait(), Ok(None)) {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Voice state kept by a page (the GTK widget's or the LineXinBar page's):
/// handles shared with the threads that record and transcribe.
#[derive(Clone, Default)]
pub struct Voice {
    arecord: Arc<Mutex<Option<Child>>>,
    running: Arc<AtomicBool>,
    whisper: Arc<Mutex<Option<Helper>>>,
    /// The Whisper model the helper holds (`_whisper_model_name`).
    whisper_model: Arc<Mutex<Option<String>>>,
    /// `_whisper_detected_lang`.
    detected: Arc<Mutex<String>>,
}

impl Voice {
    pub fn detected_lang(&self) -> String {
        self.detected
            .lock()
            .map(|lang| lang.clone())
            .unwrap_or_default()
    }

    pub fn model_loaded(&self, model: &str) -> bool {
        self.whisper_model
            .lock()
            .is_ok_and(|loaded| loaded.as_deref() == Some(model))
    }

    /// Load Whisper's `model` into the helper process (blocking; on a
    /// thread). The model then stays loaded.
    pub fn load_whisper(&self, model: &str) -> Result<(), String> {
        let mut helper = self.whisper.lock().map_err(|error| error.to_string())?;
        if helper.is_none() {
            *helper = Some(Helper::start(WHISPER_HELPER, &[]).map_err(|error| error.to_string())?);
        }
        let answer = helper
            .as_mut()
            .expect("started")
            .request(&json!({"op": "load", "model": model}));
        match answer.get("error") {
            Some(error) => Err(crate::pyrepr::str(error)),
            None => {
                if let Ok(mut loaded) = self.whisper_model.lock() {
                    *loaded = Some(model.to_owned());
                }
                Ok(())
            }
        }
    }

    /// Start `arecord` (16 kHz mono); its audio.
    pub fn start_arecord(&self) -> std::io::Result<ChildStdout> {
        let mut child = Command::new("arecord")
            .args(["-f", "S16_LE", "-c", "1", "-r", "16000", "-q"])
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        let output = child.stdout.take().expect("piped stdout");
        if let Ok(mut arecord) = self.arecord.lock() {
            *arecord = Some(child);
        }
        self.running.store(true, Ordering::SeqCst);
        Ok(output)
    }

    /// Stop listening: what was heard is thrown away.
    pub fn stop(&self) {
        self.running.store(false, Ordering::SeqCst);
        self.stop_arecord(true);
    }

    /// The original `whisper_listen_loop` (blocking; on a thread): read
    /// `audio` until two seconds of silence follow speech, then transcribe
    /// it with the loaded model. `report` hears how it goes.
    pub fn record(&self, mut audio: ChildStdout, report: impl Fn(Heard)) {
        const SAMPLE_RATE: u32 = 16000;
        const CHUNK_SIZE: usize = 4000;
        const SILENCE_THRESHOLD: f64 = 360.0;
        const SILENCE_TIMEOUT: f64 = 2.0;
        const SPEECH_CONFIRM_FRAMES: u32 = 2;
        let (running, arecord) = (&self.running, &self.arecord);
        let mut frames: Vec<u8> = Vec::new();
        let mut last_speech = Instant::now();
        let mut has_speech = false;
        let mut loud_streak = 0;
        while running.load(Ordering::SeqCst) {
            let alive = arecord
                .lock()
                .ok()
                .and_then(|mut child| {
                    child
                        .as_mut()
                        .map(|child| matches!(child.try_wait(), Ok(None)))
                })
                .unwrap_or(false);
            if !alive {
                break;
            }
            let mut data = vec![0u8; CHUNK_SIZE];
            let mut filled = 0;
            while filled < CHUNK_SIZE {
                match audio.read(&mut data[filled..]) {
                    Ok(0) => break,
                    Ok(read) => filled += read,
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
                    Err(_) => break,
                }
            }
            if filled == 0 {
                break;
            }
            data.truncate(filled);
            frames.extend_from_slice(&data);
            let rms = if filled % 2 == 0 {
                let samples: Vec<f64> = data
                    .as_chunks::<2>()
                    .0
                    .iter()
                    .map(|pair| f64::from(i16::from_le_bytes(*pair)))
                    .collect();
                (samples.iter().map(|s| s * s).sum::<f64>() / samples.len() as f64).sqrt()
            } else {
                0.0
            };
            if rms > SILENCE_THRESHOLD {
                last_speech = Instant::now();
                loud_streak += 1;
                if !has_speech && loud_streak >= SPEECH_CONFIRM_FRAMES {
                    has_speech = true;
                    report(Heard::SpeakNow);
                }
            } else {
                loud_streak = 0;
            }
            if has_speech && last_speech.elapsed().as_secs_f64() > SILENCE_TIMEOUT {
                break;
            }
        }
        if let Ok(mut arecord) = arecord.lock()
            && let Some(mut child) = arecord.take()
        {
            // SAFETY: SIGTERM to our own child.
            unsafe { libc::kill(child.id() as i32, libc::SIGTERM) };
            std::thread::spawn(move || child.wait());
        }
        if !running.load(Ordering::SeqCst) {
            report(Heard::Stopped);
            return;
        }
        if !has_speech || frames.is_empty() {
            report(Heard::Nothing);
            return;
        }
        report(Heard::Transcribing);
        let wav = crate::screen::temp_dir().join(format!(
            "linexin-stt-{}.wav",
            glib::uuid_string_random().replace('-', "")
        ));
        let written = write_wav(&wav, &frames, SAMPLE_RATE);
        let answer = match written {
            Ok(()) => self
                .whisper
                .lock()
                .ok()
                .and_then(|mut helper| {
                    helper.as_mut().map(|helper| {
                        helper.request(&json!({"op": "transcribe", "path": wav.to_string_lossy()}))
                    })
                })
                .unwrap_or_else(|| json!({"error": "no model"})),
            Err(error) => json!({"error": error.to_string()}),
        };
        let _ = std::fs::remove_file(&wav);
        if let Some(language) = answer.get("language").and_then(Value::as_str)
            && !language.is_empty()
            && let Ok(mut detected) = self.detected.lock()
        {
            *detected = language.to_owned();
        }
        let text = answer
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        if let Some(error) = answer.get("error") {
            println!("Transcription failed: {}", crate::pyrepr::str(error));
        }
        if text.is_empty() {
            report(Heard::Nothing);
        } else {
            report(Heard::Text(text));
        }
    }

    /// The original `begin_vosk_recording` loop (blocking; on a thread):
    /// `audio` streamed into Vosk, `report` hearing the words as they come,
    /// [`VoskHeard::Spoken`] after two seconds without a new one, then the
    /// final text.
    pub fn record_vosk(
        &self,
        session: VoskSession,
        mut audio: ChildStdout,
        report: impl Fn(VoskHeard),
    ) {
        let VoskSession(mut helper) = session;
        let (running, arecord) = (&self.running, &self.arecord);
        let mut last_speech = Instant::now();
        let mut last_text = String::new();
        while running.load(Ordering::SeqCst) {
            let alive = arecord
                .lock()
                .ok()
                .and_then(|mut child| {
                    child
                        .as_mut()
                        .map(|child| matches!(child.try_wait(), Ok(None)))
                })
                .unwrap_or(false);
            if !alive {
                break;
            }
            let mut data = vec![0u8; 4000];
            let mut filled = 0;
            while filled < data.len() {
                match audio.read(&mut data[filled..]) {
                    Ok(0) => break,
                    Ok(read) => filled += read,
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
                    Err(_) => break,
                }
            }
            if filled == 0 {
                break;
            }
            if helper.write(&data[..filled]).is_err() {
                break;
            }
            let answer = helper.read();
            let current = answer
                .get("text")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned();
            if !current.is_empty() && current != last_text {
                last_speech = Instant::now();
                last_text = current.clone();
                report(VoskHeard::Words(current));
            }
            if !last_text.is_empty() && last_speech.elapsed().as_secs_f64() > 2.0 {
                report(VoskHeard::Spoken);
                break;
            }
        }
        // The audio ends: the helper answers with the final text.
        helper.input.take();
        loop {
            let answer = helper.read();
            if let Some(final_text) = answer.get("final").and_then(Value::as_str) {
                if !final_text.is_empty() {
                    report(VoskHeard::Final(final_text.to_owned()));
                }
                break;
            }
            if answer.get("error").is_some() {
                break;
            }
        }
    }

    fn stop_arecord(&self, wait: bool) {
        if let Ok(mut arecord) = self.arecord.lock()
            && let Some(mut child) = arecord.take()
        {
            // SAFETY: SIGTERM to our own child, as `terminate()`.
            unsafe { libc::kill(child.id() as i32, libc::SIGTERM) };
            if wait {
                let _ = child.wait();
            } else {
                std::thread::spawn(move || child.wait());
            }
        }
    }
}

/// Where Whisper keeps `model` (`~/.cache/whisper/<model>.pt`).
pub fn whisper_model_file(model: &str) -> PathBuf {
    home().join(".cache/whisper").join(format!("{model}.pt"))
}

/// The original's download of a Whisper model: its size as the original
/// wrote it, and the shell line that fetches it (printing `NN% ...` lines)
/// after writing its script; `None` for a model it does not know.
pub fn whisper_download(model: &str) -> Option<(&'static str, String)> {
    let url = match model {
        "tiny" => {
            "https://openaipublic.azureedge.net/main/whisper/models/65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9/tiny.pt"
        }
        "base" => {
            "https://openaipublic.azureedge.net/main/whisper/models/ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e/base.pt"
        }
        "small" => {
            "https://openaipublic.azureedge.net/main/whisper/models/9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794/small.pt"
        }
        "medium" => {
            "https://openaipublic.azureedge.net/main/whisper/models/345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1/medium.pt"
        }
        _ => return None,
    };
    let size = match model {
        "tiny" => "~39 MB",
        "base" => "~74 MB",
        "small" => "~461 MB",
        _ => "~1.5 GB",
    };
    let cache = home().join(".cache/whisper");
    let script_path = crate::screen::temp_dir().join("linexin_whisper_dl.py");
    let script = format!(
        "import urllib.request, sys\nurl = '{url}'\nout = '{}'\ndef progress(block, block_size, total):\n    if total > 0:\n        pct = min(int(block * block_size * 100 / total), 100)\n        done_mb = block * block_size / 1048576\n        total_mb = total / 1048576\n        print(f'{{pct}}% {{done_mb:.0f}}MB/{{total_mb:.0f}}MB', flush=True)\nurllib.request.urlretrieve(url, out, progress)\nprint('100%', flush=True)\n",
        whisper_model_file(model).display()
    );
    if let Err(error) = std::fs::write(&script_path, script) {
        println!("Failed to write the download script: {error}");
        return None;
    }
    Some((
        size,
        format!(
            "mkdir -p {} && python3 {}",
            cache.display(),
            script_path.display()
        ),
    ))
}

/// `_play_activation_sound`: the chime that says the microphone is open.
pub fn play_activation_sound() {
    let beside = crate::widgets_dir().join("sounds/alexy_activation.ogg");
    let sound = if beside.is_file() {
        beside
    } else {
        PathBuf::from("/usr/share/linexin/widgets/sounds/alexy_activation.ogg")
    };
    if sound.is_file()
        && let Ok(mut child) = Command::new("paplay")
            .arg(&sound)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
    {
        std::thread::spawn(move || child.wait());
    }
}

/// Whether Python can import `module` (`importlib.util.find_spec`).
pub fn python_has(module: &str) -> bool {
    Command::new("python3")
        .args([
            "-c",
            "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)",
            module,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

/// Where Vosk's model for `lang` is kept.
pub fn vosk_model_dir(lang: &str) -> PathBuf {
    home().join(format!(".local/share/linexin/vosk-model-{lang}"))
}

/// The original's shell line that fetches and unpacks Vosk's model for
/// `lang`.
pub fn vosk_download(lang: &str) -> String {
    let url = format!("https://alphacephei.com/vosk/models/vosk-model-{lang}.zip");
    let unpack = format!(
        "mkdir -p ~/.local/share/linexin && rm -rf /tmp/vmodel && unzip -q -o /tmp/vmodel.zip -d /tmp/vmodel/ && mv /tmp/vmodel/* {} && rm -rf /tmp/vmodel /tmp/vmodel.zip",
        vosk_model_dir(lang).display()
    );
    format!("curl -L {url} -o /tmp/vmodel.zip && {unpack}")
}

/// Vosk loaded on a model, ready to hear ([`start_vosk`]).
pub struct VoskSession(Helper);

/// What starting Vosk came to.
pub enum VoskStart {
    Ready(VoskSession),
    /// python-vosk is not installed.
    Missing,
    /// The model could not be loaded: why.
    Failed(String),
}

/// Start the Vosk helper on `model` (blocking; on a thread).
pub fn start_vosk(model: &str) -> VoskStart {
    let mut helper = match Helper::start(VOSK_HELPER, &[model]) {
        Ok(helper) => helper,
        Err(error) => return VoskStart::Failed(error.to_string()),
    };
    let first = helper.read();
    if first.get("missing").is_some() {
        return VoskStart::Missing;
    }
    if let Some(error) = first.get("error") {
        return VoskStart::Failed(crate::pyrepr::str(error));
    }
    VoskStart::Ready(VoskSession(helper))
}

/// What streaming recognition heard ([`Voice::record_vosk`]).
pub enum VoskHeard {
    /// The words so far.
    Words(String),
    /// Two seconds without a new word after some: what was heard is meant.
    Spoken,
    /// The helper's final text once the audio ended.
    Final(String),
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(glib::home_dir)
}

/// What a recording heard, as it goes ([`Voice::record`]).
pub enum Heard {
    /// Speech began.
    SpeakNow,
    /// Silence after speech: the audio is being transcribed.
    Transcribing,
    /// What was said.
    Text(String),
    /// Nothing worth sending (no speech, or no words in it).
    Nothing,
    /// Stopped from outside ([`Voice::stop`]).
    Stopped,
}

/// What the recording thread asks of the page.
enum Event {
    Placeholder(String),
    Text(String),
    ToggleOff,
    Send,
    Voice,
}

impl Alexy {
    /// `_check_stt_availability`.
    pub fn check_stt_availability(&self) {
        let backend = self.config.borrow().stt_backend.clone();
        if backend == "whisper" && !python_has("whisper") {
            self.stt_toggle.set_sensitive(false);
            self.stt_toggle.set_tooltip_text(Some(&tr(
                "openai-whisper is not installed. Install it via: pip install openai-whisper",
            )));
            return;
        }
        if backend == "vosk" && !python_has("vosk") {
            self.stt_toggle.set_sensitive(false);
            self.stt_toggle.set_tooltip_text(Some(&tr(
                "python-vosk is not installed. You can install it from Settings.",
            )));
            return;
        }
        self.stt_toggle.set_sensitive(true);
        self.stt_toggle.set_tooltip_text(Some(""));
    }

    /// `entry.set_placeholder_text`, which the compact window watches.
    pub fn set_placeholder(&self, text: &str) {
        self.entry.set_placeholder_text(text);
        let observer = self.observer.borrow().clone();
        if let Some(observer) = observer {
            observer.placeholder(text);
        }
    }

    /// `on_stt_toggled`.
    pub fn on_stt_toggled(self: &Rc<Self>, active: bool) {
        if active {
            if self.tts_playing.get() {
                self.stop_tts();
            }
            self.voice.stop_arecord(false);
            if self.config.borrow().stt_backend == "whisper" {
                self.stt_start_whisper();
            } else {
                self.stt_start_vosk();
            }
        } else {
            self.voice.running.store(false, Ordering::SeqCst);
            self.voice.stop_arecord(true);
        }
    }

    /// `_play_activation_sound`.
    fn play_activation_sound(&self) {
        play_activation_sound();
    }

    /// `_stt_start_whisper`.
    pub fn stt_start_whisper(self: &Rc<Self>) {
        let model = self.config.borrow().whisper_model.clone();
        if self.voice.model_loaded(&model) {
            self.begin_whisper_recording();
            return;
        }
        let model_file = whisper_model_file(&model);
        if !model_file.exists() {
            self.stt_toggle.set_active(false);
            if !matches!(model.as_str(), "tiny" | "base" | "small" | "medium") {
                self.add_message_bubble(
                    "assistant",
                    &json!(tr("Unknown Whisper model: {}").replacen("{}", &model, 1)),
                );
                return;
            }
            let Some((size, command)) = whisper_download(&model) else {
                return;
            };
            let weak = Rc::downgrade(self);
            crate::progress::launch(
                self.window_or_root().as_ref(),
                &tr("Downloading Voice Recognition Model"),
                &command,
                true,
                Some(
                    &tr("Downloading Whisper {} model ({})...")
                        .replacen("{}", &model, 1)
                        .replacen("{}", size, 1),
                ),
                Some(Box::new(move |success| {
                    let Some(alexy) = weak.upgrade() else {
                        return;
                    };
                    if success {
                        let toggle = alexy.stt_toggle.downgrade();
                        glib::idle_add_local_once(move || {
                            if let Some(toggle) = toggle.upgrade() {
                                toggle.set_active(true);
                            }
                        });
                    } else {
                        alexy
                            .entry
                            .set_text(&tr("Failed to download Whisper model."));
                        let _ = std::fs::remove_file(&model_file);
                    }
                })),
                None,
                None,
            )
            .present();
            return;
        }

        self.set_placeholder(&tr("Loading Whisper model..."));
        self.stt_toggle.set_sensitive(false);
        let observer = self.observer.borrow().clone();
        if let Some(observer) = &observer {
            observer.whisper_loading();
        }
        let voice = self.voice.clone();
        let weak = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let wanted = model.clone();
            let result = gio::spawn_blocking(move || voice.load_whisper(&wanted))
                .await
                .unwrap_or_else(|_| Err("the voice helper stopped".into()));
            let Some(alexy) = weak.upgrade() else {
                return;
            };
            if let Some(observer) = &observer {
                observer.whisper_loaded(result.is_ok());
            }
            match result {
                Ok(()) => {
                    alexy.stt_toggle.set_sensitive(true);
                    if alexy.stt_toggle.is_active() {
                        guard("record", || alexy.begin_whisper_recording());
                    } else {
                        alexy.set_placeholder(&tr("Ask a question..."));
                    }
                }
                Err(error) => {
                    alexy.stt_toggle.set_sensitive(true);
                    alexy.add_message_bubble(
                        "assistant",
                        &json!(format!("{}{error}", tr("Error loading Whisper model: "))),
                    );
                    alexy.stt_toggle.set_active(false);
                    alexy.set_placeholder(&tr("Ask a question..."));
                }
            }
        });
    }

    /// Apply what a recording thread reports.
    fn listen(self: &Rc<Self>, mut events: futures_channel::mpsc::UnboundedReceiver<Event>) {
        let weak = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            use futures_util::StreamExt;
            while let Some(event) = events.next().await {
                let Some(alexy) = weak.upgrade() else {
                    break;
                };
                match event {
                    Event::Placeholder(text) => alexy.set_placeholder(&text),
                    Event::Text(text) => alexy.entry.set_text(&text),
                    Event::ToggleOff => alexy.stt_toggle.set_active(false),
                    Event::Send => alexy.send_btn.emit_clicked(),
                    Event::Voice => alexy.last_input_was_voice.set(true),
                }
            }
        });
    }

    /// `_begin_whisper_recording`: record until two seconds of silence after
    /// speech, then transcribe and send.
    fn begin_whisper_recording(self: &Rc<Self>) {
        self.set_placeholder(&tr("Listening..."));
        self.play_activation_sound();
        let audio = match self.voice.start_arecord() {
            Ok(audio) => audio,
            Err(error) => {
                self.add_message_bubble(
                    "assistant",
                    &json!(tr("Failed to start mic: {}").replacen("{}", &error.to_string(), 1)),
                );
                self.stt_toggle.set_active(false);
                return;
            }
        };
        let (sender, events) = futures_channel::mpsc::unbounded();
        self.listen(events);
        let voice = self.voice.clone();
        let (speak_now, ask, transcribing) = (
            tr("Listening... (speak now)"),
            tr("Ask a question..."),
            tr("Transcribing..."),
        );
        std::thread::spawn(move || {
            let send = |event| {
                let _ = sender.unbounded_send(event);
            };
            voice.record(audio, |heard| match heard {
                Heard::SpeakNow => send(Event::Placeholder(speak_now.clone())),
                Heard::Transcribing => send(Event::Placeholder(transcribing.clone())),
                Heard::Stopped => send(Event::Placeholder(ask.clone())),
                Heard::Nothing => {
                    send(Event::Placeholder(ask.clone()));
                    send(Event::ToggleOff);
                }
                Heard::Text(text) => {
                    send(Event::Voice);
                    send(Event::Placeholder(ask.clone()));
                    send(Event::Text(text));
                    send(Event::ToggleOff);
                    send(Event::Send);
                }
            });
        });
    }

    /// `_stt_start_vosk`: streaming recognition, sent after two seconds
    /// without a new word.
    fn stt_start_vosk(self: &Rc<Self>) {
        let lang = self.config.borrow().vosk_lang.clone();
        let model_path = vosk_model_dir(&lang);
        if !model_path.exists() {
            self.stt_toggle.set_active(false);
            let weak = Rc::downgrade(self);
            crate::progress::launch(
                self.window_or_root().as_ref(),
                &tr("Downloading Offline Voice Model"),
                &vosk_download(&lang),
                false,
                None,
                Some(Box::new(move |success| {
                    if let Some(alexy) = weak.upgrade() {
                        alexy.entry.set_text(&if success {
                            tr("Model downloaded. Click mic to speak.")
                        } else {
                            tr("Failed to download voice model.")
                        });
                    }
                })),
                None,
                None,
            )
            .present();
            return;
        }
        let model = model_path.to_string_lossy().into_owned();
        let weak = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let started = gio::spawn_blocking(move || start_vosk(&model)).await;
            let Some(alexy) = weak.upgrade() else {
                return;
            };
            match started {
                Ok(VoskStart::Ready(session)) => {
                    guard("vosk", || alexy.begin_vosk_recording(session));
                }
                Ok(VoskStart::Missing) => {
                    alexy.add_message_bubble(
                        "assistant",
                        &json!(tr(
                            "python-vosk is not installed. You can install it from Settings."
                        )),
                    );
                    alexy.stt_toggle.set_active(false);
                }
                Ok(VoskStart::Failed(error)) => {
                    alexy.add_message_bubble(
                        "assistant",
                        &json!(tr("Error loading voice model: {}").replacen("{}", &error, 1)),
                    );
                    alexy.stt_toggle.set_active(false);
                }
                Err(_) => {}
            }
        });
    }

    fn begin_vosk_recording(self: &Rc<Self>, session: VoskSession) {
        self.set_placeholder(&tr("Listening..."));
        self.play_activation_sound();
        let audio = match self.voice.start_arecord() {
            Ok(audio) => audio,
            Err(error) => {
                self.add_message_bubble(
                    "assistant",
                    &json!(tr("Failed to start mic: {}").replacen("{}", &error.to_string(), 1)),
                );
                self.voice.running.store(false, Ordering::SeqCst);
                self.stt_toggle.set_active(false);
                return;
            }
        };
        self.voice.running.store(true, Ordering::SeqCst);
        let (sender, events) = futures_channel::mpsc::unbounded();
        self.listen(events);
        let voice = self.voice.clone();
        let ask = tr("Ask a question...");
        std::thread::spawn(move || {
            let send = |event| {
                let _ = sender.unbounded_send(event);
            };
            voice.record_vosk(session, audio, |heard| match heard {
                VoskHeard::Words(text) | VoskHeard::Final(text) => send(Event::Text(text)),
                VoskHeard::Spoken => {
                    send(Event::Voice);
                    send(Event::ToggleOff);
                    send(Event::Send);
                }
            });
            send(Event::Placeholder(ask));
        });
    }

    /// The "Hey Alexy" switch: the autostart entry and the daemon.
    pub fn on_hey_linux_toggled(&self, enabled: bool) {
        self.config.borrow_mut().hey_linux_enabled = enabled;
        self.config.borrow().save();
        set_hey_linux(enabled);
    }

    /// SIGUSR1: restart voice input (the original's `_retoggle(stt_toggle)`).
    pub fn reactivate_voice(&self) {
        if self.stt_toggle.is_active() {
            self.stt_toggle.set_active(false);
        }
        self.stt_toggle.set_active(true);
    }
}

/// The "Hey Alexy" daemon on or off: its autostart entry, and the daemon
/// (re)started or stopped.
pub fn set_hey_linux(enabled: bool) {
    let autostart = home().join(".config/autostart");
    let desktop_file = autostart.join("hey-linux.desktop");
    if enabled {
        let _ = std::fs::create_dir_all(&autostart);
        let _ = std::fs::write(
            &desktop_file,
            "[Desktop Entry]\nName=Hey Alexy Wake Word\nExec=/usr/bin/hey-linux\nType=Application\nNoDisplay=true\n",
        );
        kill_hey_linux();
        launch_hey_linux_detached();
    } else {
        if desktop_file.exists() {
            let _ = std::fs::remove_file(&desktop_file);
        }
        kill_hey_linux();
    }
}

/// `_kill_hey_linux`.
pub fn kill_hey_linux() {
    let _ = Command::new("sh")
        .args([
            "-c",
            "pkill -9 -f '[/]usr/bin/hey-linux' 2>/dev/null; pkill -9 -f '[h]ey-linux-venv/bin/python' 2>/dev/null",
        ])
        .status();
}

/// `_launch_hey_linux_detached`: fully detached (double fork).
pub fn launch_hey_linux_detached() {
    let _ = Command::new("sh")
        .args([
            "-c",
            "setsid /usr/bin/hey-linux </dev/null >/dev/null 2>&1 &",
        ])
        .status();
}

/// 16-bit mono PCM as a WAV file.
fn write_wav(path: &std::path::Path, data: &[u8], rate: u32) -> std::io::Result<()> {
    let mut file = std::fs::File::create(path)?;
    let size = data.len() as u32;
    file.write_all(b"RIFF")?;
    file.write_all(&(36 + size).to_le_bytes())?;
    file.write_all(b"WAVEfmt ")?;
    file.write_all(&16u32.to_le_bytes())?;
    file.write_all(&1u16.to_le_bytes())?;
    file.write_all(&1u16.to_le_bytes())?;
    file.write_all(&rate.to_le_bytes())?;
    file.write_all(&(rate * 2).to_le_bytes())?;
    file.write_all(&2u16.to_le_bytes())?;
    file.write_all(&16u16.to_le_bytes())?;
    file.write_all(b"data")?;
    file.write_all(&size.to_le_bytes())?;
    file.write_all(data)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wav_files_are_what_python_wave_writes() {
        let path = std::env::temp_dir().join(format!("alexy-wav-{}.wav", std::process::id()));
        write_wav(&path, &[1, 0, 255, 255, 0, 1], 16000).unwrap();
        let Ok(output) = Command::new("python3")
            .args([
                "-c",
                "import wave,sys; w=wave.open(sys.argv[1]); print(w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes())",
            ])
            .arg(&path)
            .output()
        else {
            return;
        };
        std::fs::remove_file(&path).unwrap();
        assert_eq!(
            String::from_utf8_lossy(&output.stdout).trim(),
            "1 2 16000 3"
        );
    }
}
