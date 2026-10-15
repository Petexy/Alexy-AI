//! Speaking replies: the original's `play_tts` (Piper, espeak-ng for the
//! languages Piper lacks) and `_stop_tts`.

use crate::ui::{Alexy, guard, tr};
use gtk::glib;
use gtk::prelude::*;
use regex::Regex;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::Command;
use std::rc::Rc;
use std::sync::LazyLock;
use std::time::Duration;

const FALLBACK: (&str, &str) = ("en_US-libritts_r-medium", "en/en_US/libritts_r/medium");

/// The Piper voice for a Vosk language model.
fn voice_for_vosk(lang: &str) -> (&'static str, &'static str) {
    match lang {
        "small-en-us-0.15" => ("en_US-libritts_r-medium", "en/en_US/libritts_r/medium"),
        "small-en-in-0.4" => ("en_GB-alba-medium", "en/en_GB/alba/medium"),
        "small-cn-0.22" => ("zh_CN-huayan-medium", "zh/zh_CN/huayan/medium"),
        "small-fr-0.22" => ("fr_FR-siwis-low", "fr/fr_FR/siwis/low"),
        "small-de-0.15" => ("de_DE-thorsten-medium", "de/de_DE/thorsten/medium"),
        "small-es-0.42" => ("es_ES-sharvard-medium", "es/es_ES/sharvard/medium"),
        "small-pt-0.3" => ("pt_PT-tugao-medium", "pt/pt_PT/tugao/medium"),
        "small-it-0.22" => ("it_IT-riccardo-x_low", "it/it_IT/riccardo/x_low"),
        "small-ru-0.22" => ("ru_RU-denis-medium", "ru/ru_RU/denis/medium"),
        "small-uk-v3-nano" => ("uk_UA-ukromir-medium", "uk/uk_UA/ukromir/medium"),
        "small-pl-0.22" => ("pl_PL-gosia-medium", "pl/pl_PL/gosia/medium"),
        "small-ja-0.22" => ("ESPEAK", "ja"),
        "small-ko-0.22" => ("ESPEAK", "ko"),
        _ => FALLBACK,
    }
}

/// The Piper voice for a language Whisper detected.
fn voice_for_whisper(lang: &str) -> (&'static str, &'static str) {
    match lang {
        "en" => ("en_US-libritts_r-medium", "en/en_US/libritts_r/medium"),
        "zh" => ("zh_CN-huayan-medium", "zh/zh_CN/huayan/medium"),
        "fr" => ("fr_FR-siwis-low", "fr/fr_FR/siwis/low"),
        "de" => ("de_DE-thorsten-medium", "de/de_DE/thorsten/medium"),
        "es" => ("es_ES-sharvard-medium", "es/es_ES/sharvard/medium"),
        "pt" => ("pt_PT-tugao-medium", "pt/pt_PT/tugao/medium"),
        "it" => ("it_IT-riccardo-x_low", "it/it_IT/riccardo/x_low"),
        "ru" => ("ru_RU-denis-medium", "ru/ru_RU/denis/medium"),
        "uk" => ("uk_UA-ukromir-medium", "uk/uk_UA/ukromir/medium"),
        "pl" => ("pl_PL-gosia-medium", "pl/pl_PL/gosia/medium"),
        "ja" => ("ESPEAK", "ja"),
        "ko" => ("ESPEAK", "ko"),
        _ => FALLBACK,
    }
}

/// The reply as it is read aloud: no code blocks or Markdown marks, one line.
pub fn clean_text(text: &str) -> String {
    static CODE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?s)```.*?```").expect("valid"));
    static MARKS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[*_~`#>]").expect("valid"));
    static NEWLINES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\n+").expect("valid"));
    static SPACES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s{2,}").expect("valid"));
    let text = CODE.replace_all(text, "");
    let text = MARKS.replace_all(&text, "");
    let text = NEWLINES.replace_all(&text, " ");
    let text = SPACES.replace_all(&text, " ");
    crate::conversations::python_strip(&text).to_owned()
}

/// How a reply is read aloud ([`plan`]).
pub enum Speech {
    /// espeak-ng, for a language Piper has no voice for: the voice and the
    /// cleaned text.
    Espeak { voice: String, text: String },
    /// Piper: the shell line that speaks, and the one that fetches the
    /// engine or the voice first where they are missing.
    Piper {
        line: String,
        download: Option<String>,
    },
}

/// What the original `play_tts` does with `text`: `None` when nothing is
/// left to say once code blocks and Markdown marks are gone. The voice is
/// the language Whisper heard (with the Whisper backend), otherwise the Vosk
/// model's.
pub fn plan(text: &str, stt_backend: &str, vosk_lang: &str, detected: &str) -> Option<Speech> {
    let clean = clean_text(text);
    if clean.is_empty() {
        return None;
    }
    let (model_name, model_path) = if stt_backend == "whisper" && !detected.is_empty() {
        voice_for_whisper(detected)
    } else {
        voice_for_vosk(vosk_lang)
    };
    if model_name == "ESPEAK" {
        return Some(Speech::Espeak {
            voice: model_path.to_owned(),
            text: clean,
        });
    }
    let piper = home().join(".local/share/linexin/piper/piper");
    let model_file = home().join(format!(
        ".local/share/linexin/piper-models/{model_name}.onnx"
    ));
    let line = format!(
        "echo {} | {} --model {} --output_file - | aplay -q",
        shell_quote(&clean),
        piper.display(),
        model_file.display()
    );
    let needs_piper = !piper.exists();
    let needs_model = !model_file.exists();
    let download = (needs_piper || needs_model).then(|| {
        let mut commands =
            vec!["mkdir -p ~/.local/share/linexin/piper ~/.local/share/linexin/piper-models".to_owned()];
        if needs_piper {
            commands.push("curl -sL https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz -o /tmp/piper.tar.gz".into());
            commands.push("tar -xzf /tmp/piper.tar.gz -C ~/.local/share/linexin/".into());
            commands.push("rm -f /tmp/piper.tar.gz".into());
        }
        if needs_model {
            let base = format!(
                "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/{model_path}/{model_name}.onnx"
            );
            commands.push(format!("curl -sL {base} -o {}", model_file.display()));
            commands.push(format!("curl -sL {base}.json -o {}.json", model_file.display()));
        }
        commands.join(" && ")
    });
    Some(Speech::Piper { line, download })
}

/// A speaking process, in its own session so it can be stopped whole.
pub fn speaking(mut command: Command) -> std::io::Result<std::process::Child> {
    // SAFETY: setsid is async-signal-safe (preexec_fn=os.setsid).
    unsafe {
        command.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    command.spawn()
}

/// Stop a speaking process and everything it started.
pub fn silence(child: std::process::Child) {
    let pid = child.id() as i32;
    // SAFETY: the speaking process leads its own group (setsid).
    let group = unsafe { libc::getpgid(pid) };
    if group > 0 {
        // SAFETY: SIGKILL to that group.
        unsafe { libc::killpg(group, libc::SIGKILL) };
    } else {
        // SAFETY: SIGKILL to the process.
        unsafe { libc::kill(pid, libc::SIGKILL) };
    }
    std::mem::forget(child);
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(glib::home_dir)
}

/// `shlex.quote`.
pub fn shell_quote(text: &str) -> String {
    if text.is_empty() {
        return "''".into();
    }
    if text
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || "@%+=:,./-_".contains(c))
    {
        return text.to_owned();
    }
    format!("'{}'", text.replace('\'', "'\"'\"'"))
}

impl Alexy {
    /// The original `play_tts`; `on_ready` runs once speech is about to start
    /// (or there is nothing to say).
    pub fn play_tts(self: &Rc<Self>, text: &str, on_ready: Option<Box<dyn FnOnce()>>) {
        let ready = move || {
            if let Some(on_ready) = on_ready {
                glib::idle_add_local_once(on_ready);
            }
        };
        let speech = {
            let config = self.config.borrow();
            plan(
                text,
                &config.stt_backend,
                &config.vosk_lang,
                &self.voice.detected_lang(),
            )
        };
        match speech {
            None => ready(),
            Some(Speech::Espeak { voice, text }) => {
                let weak = Rc::downgrade(self);
                let mut ready = Some(ready);
                glib::timeout_add_local_once(Duration::from_millis(100), move || {
                    let Some(alexy) = weak.upgrade() else {
                        return;
                    };
                    println!("Executing fallback TTS (espeak-ng): -v {voice}");
                    let mut command = Command::new("espeak-ng");
                    command.args(["-v", &voice, &text]);
                    alexy.start_speaking(command);
                    if let Some(ready) = ready.take() {
                        ready();
                    }
                });
                self.notify_tts();
            }
            Some(Speech::Piper {
                line,
                download: Some(download),
            }) => {
                let weak = Rc::downgrade(self);
                crate::progress::launch(
                    self.window_or_root().as_ref(),
                    &tr("Downloading Neural TTS Engine/Voice"),
                    &download,
                    false,
                    None,
                    Some(Box::new(move |success| {
                        ready();
                        if success {
                            glib::timeout_add_local_once(Duration::from_millis(1000), move || {
                                if let Some(alexy) = weak.upgrade() {
                                    alexy.run_piper(&line);
                                }
                            });
                        }
                    })),
                    None,
                    None,
                )
                .present();
                self.notify_tts();
            }
            Some(Speech::Piper {
                line,
                download: None,
            }) => {
                ready();
                self.run_piper(&line);
                self.notify_tts();
            }
        }
    }

    fn run_piper(self: &Rc<Self>, command_line: &str) {
        println!("Executing TTS: {command_line}");
        let mut command = Command::new("bash");
        command.args(["-c", command_line]);
        self.start_speaking(command);
    }

    /// Start a speaking process in its own session and wait for it.
    fn start_speaking(self: &Rc<Self>, command: Command) {
        let child = match speaking(command) {
            Ok(child) => child,
            Err(error) => {
                println!("TTS failed: {error}");
                return;
            }
        };
        let pid = child.id();
        self.tts_process.replace(Some(child));
        self.tts_playing.set(true);
        self.send_btn.set_icon_name(&self.icon_stop);
        self.stt_toggle.set_sensitive(false);
        let weak = Rc::downgrade(self);
        glib::spawn_future_local(async move {
            let _ = gtk::gio::spawn_blocking(move || {
                let mut status = 0;
                // SAFETY: waiting for our own child by pid.
                unsafe { libc::waitpid(pid as i32, &mut status, 0) };
            })
            .await;
            if let Some(alexy) = weak.upgrade() {
                let current = alexy
                    .tts_process
                    .borrow()
                    .as_ref()
                    .map(std::process::Child::id);
                if current == Some(pid) {
                    alexy.tts_process.replace(None);
                }
                guard("stop speaking", || alexy.stop_tts());
            }
        });
    }

    fn notify_tts(self: &Rc<Self>) {
        let observer = self.observer.borrow().clone();
        if let Some(observer) = observer {
            let weak = Rc::downgrade(self);
            glib::timeout_add_local_once(Duration::from_millis(250), move || {
                if let Some(alexy) = weak.upgrade()
                    && alexy.tts_playing.get()
                {
                    observer.tts_started();
                }
            });
        }
    }

    /// The original `_stop_tts`.
    pub fn stop_tts(&self) {
        if let Some(child) = self.tts_process.take() {
            silence(child);
        }
        self.tts_playing.set(false);
        self.send_btn.set_icon_name(&self.icon_send);
        self.stt_toggle.set_sensitive(true);
        self.new_conv_btn.set_sensitive(true);
        self.conv_toggle_btn.set_sensitive(true);
        self.settings_btn.set_sensitive(true);
        self.entry.set_sensitive(true);
        self.entry.grab_focus();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replies_are_cleaned_for_speech() {
        assert_eq!(
            clean_text("# Title\n\n**Bold** _it_\n```bash\nls\n```\nend  >  x"),
            "Title Bold it end x"
        );
        assert_eq!(shell_quote("it's"), "'it'\"'\"'s'");
    }
}
