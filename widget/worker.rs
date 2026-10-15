//! One chat turn on a worker thread: the original's `call_ai` and the rest
//! of `_execute_urllib_request`, with `_run_autonomous_commands`. What the
//! page must do is sent to it as [`ToUi`] (the original's `GLib.idle_add`).

use crate::ai::{self, Outcome, Target};
use crate::ui::tr;
use futures_channel::mpsc::UnboundedSender;
use linexin_plugin_api::Host;
use regex::Regex;
use serde_json::{Value, json};
use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

/// What the worker asks of the GTK side.
pub enum ToUi {
    StreamBegin,
    StreamPush(String, String),
    StreamFinish {
        reply: String,
        speak: bool,
        final_: bool,
    },
    StreamDiscard,
    ApiError(String),
    /// An assistant bubble (e.g. "Model ... not found locally").
    Bubble(String),
    /// Ollama lacks the model: pull it, then try again.
    PullModel(String),
    /// Ollama is not installed: offer to install it, then try again.
    InstallOllama,
    /// The endpoint's model was detected (subtitle and config follow).
    EndpointModel(String),
    /// Ask before running a command (auto-execution off).
    AskSafety {
        code: String,
        reply: mpsc::Sender<bool>,
    },
    /// Ask for the password before a `sudo` command.
    AskPassword {
        message: String,
        reply: mpsc::Sender<bool>,
    },
}

/// The settings a turn reads (the original read them from `self`).
#[derive(Clone)]
pub struct Settings {
    pub backend: String,
    pub api_url: String,
    pub api_key: String,
    pub model: String,
    pub local_model: String,
    pub local_url: String,
    pub endpoint_url: String,
    pub endpoint_model: String,
    pub auto_execute_commands: bool,
}

/// What a turn shares with the page.
pub struct Shared {
    pub history: Mutex<Vec<Value>>,
    pub abort: AtomicBool,
    /// `_speak_next_response`.
    pub speak_next: AtomicBool,
}

pub struct Turn {
    pub settings: Settings,
    pub shared: Arc<Shared>,
    pub host: Option<Host>,
    pub ui: UnboundedSender<ToUi>,
    /// Run `sudo` commands through pkexec (the session's polkit agent asks)
    /// instead of the Center's sudo wrapper: the LineXinBar page, which has
    /// no password field.
    pub pkexec: bool,
}

impl Turn {
    fn send(&self, message: ToUi) {
        let _ = self.ui.unbounded_send(message);
    }

    fn aborted(&self) -> bool {
        self.shared.abort.load(Ordering::SeqCst)
    }

    /// The original `call_ai`.
    pub fn call_ai(&mut self) {
        match self.settings.backend.as_str() {
            "direct" => self.call_direct_api(),
            "local" => self.call_local_ollama(),
            "endpoint" => self.call_endpoint_api(),
            _ => {}
        }
    }

    fn call_direct_api(&mut self) {
        let target = Target::Direct {
            url: self.settings.api_url.clone(),
            key: self.settings.api_key.clone(),
            model: self.settings.model.clone(),
        };
        self.execute(&target);
    }

    fn call_endpoint_api(&mut self) {
        if self.settings.endpoint_url.is_empty() {
            self.send(ToUi::ApiError(tr(
                "No endpoint URL configured. Please open settings and set the Endpoint URL.",
            )));
            return;
        }
        if self.settings.endpoint_model.is_empty()
            && let Some(detected) = ai::discover_endpoint_model(&self.settings.endpoint_url)
        {
            self.settings.endpoint_model = detected.clone();
            self.send(ToUi::EndpointModel(detected));
        }
        let target = Target::Endpoint {
            url: self.settings.endpoint_url.clone(),
            model: self.settings.endpoint_model.clone(),
        };
        self.execute(&target);
    }

    fn call_local_ollama(&mut self) {
        if !ollama_installed() {
            self.send(ToUi::InstallOllama);
            return;
        }
        if self.settings.local_model.is_empty() {
            self.send(ToUi::ApiError(tr(
                "No local model selected. Please open settings and pull an AI model.",
            )));
            return;
        }
        let target = Target::Ollama {
            url: self.settings.local_url.clone(),
            model: self.settings.local_model.clone(),
        };
        self.execute(&target);
    }

    /// The original `_execute_urllib_request`.
    fn execute(&mut self, target: &Target) {
        let history = self
            .shared
            .history
            .lock()
            .map(|h| h.clone())
            .unwrap_or_default();
        let ui = self.ui.clone();
        let outcome = ai::chat(
            target,
            &history,
            &self.shared.abort,
            &|| {
                let _ = ui.unbounded_send(ToUi::StreamBegin);
            },
            &|content, reasoning| {
                let _ =
                    ui.unbounded_send(ToUi::StreamPush(content.to_owned(), reasoning.to_owned()));
            },
        );
        match outcome {
            Outcome::Done { content } => {
                if self.aborted() {
                    self.send(ToUi::StreamDiscard);
                    return;
                }
                static THINK: LazyLock<Regex> =
                    LazyLock::new(|| Regex::new(r"(?s)<think>.*?</think>").expect("valid"));
                let reply =
                    crate::conversations::python_strip(&THINK.replace_all(&content, "")).to_owned();
                if let Ok(mut history) = self.shared.history.lock() {
                    history.push(json!({"role": "assistant", "content": reply}));
                }
                static COMMAND: LazyLock<Regex> =
                    LazyLock::new(|| Regex::new(r"(?s)```(?:bash|sh)\n.*?```").expect("valid"));
                if COMMAND.is_match(&reply) {
                    self.send(ToUi::StreamFinish {
                        reply: reply.clone(),
                        speak: false,
                        final_: false,
                    });
                    if self.run_autonomous_commands(&reply) {
                        return;
                    }
                }
                if self.aborted() {
                    return;
                }
                let speak = self.shared.speak_next.load(Ordering::SeqCst);
                self.send(ToUi::StreamFinish {
                    reply,
                    speak,
                    final_: true,
                });
            }
            Outcome::Aborted => self.send(ToUi::StreamDiscard),
            Outcome::Http { code, reason, body } => {
                if target.is_ollama() && code == 404 {
                    self.send(ToUi::StreamDiscard);
                    self.send(ToUi::Bubble(
                        tr("Model '{}' not found locally. Downloading now...").replacen(
                            "{}",
                            &self.settings.local_model,
                            1,
                        ),
                    ));
                    self.send(ToUi::PullModel(self.settings.local_model.clone()));
                    return;
                }
                self.send(ToUi::StreamDiscard);
                self.send(ToUi::ApiError(format!(
                    "HTTP Error {code}: {reason}\n{body}"
                )));
            }
            Outcome::Connection(error) => {
                let mut message = format!("Connection Failed: {error}");
                if target.is_ollama() {
                    message.push_str(
                        "\nIs the ollama.service running? Try: `systemctl enable --now ollama`",
                    );
                }
                self.send(ToUi::StreamDiscard);
                self.send(ToUi::ApiError(message));
            }
            Outcome::OllamaError(error) => {
                self.send(ToUi::StreamDiscard);
                self.send(ToUi::ApiError(error));
            }
            Outcome::Unexpected(shown) => {
                self.send(ToUi::StreamDiscard);
                self.send(ToUi::ApiError(
                    tr("Unexpected response from server: {}").replacen("{}", &shown, 1),
                ));
            }
            Outcome::Other(message) => {
                self.send(ToUi::StreamDiscard);
                self.send(ToUi::ApiError(message));
            }
        }
    }

    /// Ask the page something and wait for the answer.
    fn ask(&self, make: impl FnOnce(mpsc::Sender<bool>) -> ToUi) -> bool {
        let (reply, answer) = mpsc::channel();
        self.send(make(reply));
        answer.recv().unwrap_or(false)
    }

    /// The original `_run_autonomous_commands`: every ```bash block run (after
    /// asking, with auto-execution off), the results handed back to the model.
    fn run_autonomous_commands(&mut self, reply: &str) -> bool {
        static BLOCKS: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"(?s)```(?:bash|sh)\n(.*?)```").expect("valid"));
        static POWER: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(
                r"\bsudo\s+((?:systemctl\s+)?(?:poweroff|reboot|halt|suspend|hibernate)|shutdown(?:\s+\S+)*)",
            )
            .expect("valid")
        });
        let blocks: Vec<String> = BLOCKS
            .captures_iter(reply)
            .map(|captures| captures[1].to_owned())
            .collect();
        if blocks.is_empty() {
            return false;
        }
        let mut full_output = String::new();
        for code in blocks {
            if self.aborted() {
                break;
            }
            if !self.settings.auto_execute_commands {
                let allowed = self.ask(|reply| ToUi::AskSafety {
                    code: code.clone(),
                    reply,
                });
                if !allowed {
                    full_output.push_str(&format!(
                        "Command:\n{code}\nExit Code: Denied\n\nSTDERR:\nThe user explicitly denied permission to run this command. You must think of another way or ask the user for clarification.\n\n---\n\n"
                    ));
                    continue;
                }
            }
            // logind lets the session's user power off without sudo.
            let code = POWER.replace_all(&code, "${1}").into_owned();
            let mut privileged = false;
            let code_to_run = match self.host {
                _ if self.pkexec && code.contains("sudo ") => {
                    println!("[DEBUG - AI Sysadmin] Executing autonomous SUDO command:\n{code}\n");
                    code.replace("sudo ", "pkexec ")
                }
                Some(host) if code.contains("sudo ") => {
                    if !host.sudo_has_password() {
                        let message = tr(
                            "The assistant wants to run a privileged command:\n\n{0}\n\nPlease authenticate.",
                        )
                        .replacen("{0}", &code, 1);
                        let authenticated = self.ask(|reply| ToUi::AskPassword { message, reply });
                        if !authenticated {
                            full_output.push_str(&format!(
                                "Command:\n{code}\nExit Code: Exception\n\nSTDERR:\nUser cancelled sudo authentication.\n\n---\n\n"
                            ));
                            continue;
                        }
                    }
                    let to_run =
                        code.replace("sudo ", &format!("\"{}\" ", host.sudo_wrapper_path()));
                    host.sudo_start_session();
                    privileged = true;
                    println!("[DEBUG - AI Sysadmin] Executing autonomous SUDO command:\n{code}\n");
                    to_run
                }
                _ => {
                    println!(
                        "[DEBUG - AI Sysadmin] Executing autonomous non-sudo command:\n{code}\n"
                    );
                    code.clone()
                }
            };
            full_output.push_str(&self.run_command(&code, &code_to_run));
            if privileged && let Some(host) = self.host {
                host.sudo_stop_session();
                host.sudo_forget_password();
            }
        }
        if !full_output.trim().is_empty() {
            let message = format!(
                "System Command Execution Results:\n\n{}\n\nPlease analyze the output and continue the task, or state that the task is complete.",
                crate::conversations::python_strip(&full_output)
            );
            if let Ok(mut history) = self.shared.history.lock() {
                history.push(json!({"role": "user", "content": message}));
            }
            match self.settings.backend.as_str() {
                "local" => self.call_local_ollama(),
                "endpoint" => self.call_endpoint_api(),
                _ => self.call_direct_api(),
            }
        }
        true
    }

    /// One command under `bash -c`, both streams read, 120 seconds at most.
    fn run_command(&self, code: &str, code_to_run: &str) -> String {
        let spawned = (|| -> std::io::Result<_> {
            let (reader, writer) = std::io::pipe()?;
            let error_writer = writer.try_clone()?;
            let child = Command::new("bash")
                .arg("-c")
                .arg(code_to_run)
                .stdin(Stdio::null())
                .stdout(writer)
                .stderr(error_writer)
                .spawn()?;
            Ok((reader, child))
        })();
        let (mut reader, mut child) = match spawned {
            Ok(spawned) => spawned,
            Err(error) => {
                return format!(
                    "Command:\n{code}\nExit Code: Exception\n\nSTDERR:\n{error}\n\n---\n\n"
                );
            }
        };
        let (lines_sender, lines) = mpsc::channel::<String>();
        let reading = std::thread::spawn(move || {
            let mut buffer = [0u8; 4096];
            let mut pending = Vec::new();
            loop {
                match reader.read(&mut buffer) {
                    Ok(0) | Err(_) => break,
                    Ok(read) => {
                        pending.extend_from_slice(&buffer[..read]);
                        while let Some(newline) = pending.iter().position(|byte| *byte == b'\n') {
                            let line: Vec<u8> = pending.drain(..=newline).collect();
                            let _ = lines_sender.send(String::from_utf8_lossy(&line).into_owned());
                        }
                    }
                }
            }
            if !pending.is_empty() {
                let _ = lines_sender.send(String::from_utf8_lossy(&pending).into_owned());
            }
        });
        let start = Instant::now();
        let mut output = String::new();
        let mut timed_out = false;
        let mut aborted = false;
        loop {
            if self.aborted() {
                // SAFETY: a plain signal to our own child.
                unsafe { libc::kill(child.id() as i32, libc::SIGTERM) };
                aborted = true;
                break;
            }
            if start.elapsed() > Duration::from_secs(120) {
                let _ = child.kill();
                timed_out = true;
                break;
            }
            match lines.recv_timeout(Duration::from_millis(100)) {
                Ok(line) => {
                    print!("{line}");
                    output.push_str(&line);
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    if matches!(child.try_wait(), Ok(Some(_))) {
                        break;
                    }
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
        let status = child.wait();
        let _ = reading.join();
        for line in lines.try_iter() {
            print!("{line}");
            output.push_str(&line);
        }
        let _ = std::io::Write::flush(&mut std::io::stdout());
        let combined = crate::conversations::python_strip(&output);
        if aborted || self.aborted() {
            return format!(
                "Command:\n{code}\nExit Code: Aborted\n\nSTDERR:\nUser cancelled the generation.\n\n---\n\n"
            );
        }
        if timed_out {
            return format!(
                "Command:\n{code}\nExit Code: TimeoutExpired\n\nSTDERR:\nThe command took longer than 120 seconds and was terminated.\n\n---\n\n"
            );
        }
        let returncode = match status {
            Ok(status) => {
                use std::os::unix::process::ExitStatusExt;
                status
                    .code()
                    .unwrap_or_else(|| -status.signal().unwrap_or(0))
            }
            Err(error) => {
                return format!(
                    "Command:\n{code}\nExit Code: Exception\n\nSTDERR:\n{error}\n\n---\n\n"
                );
            }
        };
        let mut result = format!("Command:\n{code}\nExit Code: {returncode}");
        if !combined.is_empty() {
            result.push_str(&format!("\n\nOUTPUT:\n{combined}"));
        }
        println!("\n[DEBUG - AI Sysadmin] Exit Code: {returncode}");
        println!("{}", "-".repeat(40));
        result + "\n\n---\n\n"
    }
}

/// `shutil.which("ollama") is not None`.
pub fn ollama_installed() -> bool {
    std::env::var_os("PATH").is_some_and(|path| {
        std::env::split_paths(&path).any(|dir| {
            use std::os::unix::fs::PermissionsExt;
            std::fs::metadata(dir.join("ollama")).is_ok_and(|metadata| {
                metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
            })
        })
    })
}
