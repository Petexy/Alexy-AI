//! Alexy's settings (`~/.config/linexin/ai-sysadmin.json`) and agents: the
//! original's `load_config`, `save_config`, `_migrate_legacy_config` and the
//! agent helpers.

use crate::pyjson;
use crate::ui::tr;
use serde_json::{Map, Value, json};
use std::path::PathBuf;

/// Name of the built-in, non-editable agent (a stable key, not translated).
pub const DEFAULT_AGENT_NAME: &str = "Alexy";
/// The built-in agent that answers in rhyming verse.
pub const RHYMEXY_AGENT_NAME: &str = "Rhymexy";
pub const BUILTIN_AGENT_NAMES: [&str; 2] = [DEFAULT_AGENT_NAME, RHYMEXY_AGENT_NAME];

pub const RHYMEXY_PROMPT: &str = "You are Rhymexy.

Your purpose is to answer every user request in rhyming verse.

Rules:

1. Every response must rhyme.
2. Always rhyme in the same language the user is writing in. Detect the user's language from their message and compose your rhymes in that language, never defaulting to English unless the user writes in English.
3. Preserve factual accuracy.
4. Never sacrifice correctness solely for rhyme.
5. Use couplets, quatrains, or longer rhyme schemes as needed.
6. Maintain a natural flow and readability.
7. Never explain your rhyming behavior unless explicitly asked.
8. For technical topics:
 - Explain concepts using rhyme.
 - Use precise terminology even if some lines do not rhyme perfectly.
9. For code requests:
 - Introduce the solution with rhyming text.
 - Output code exactly as needed in code blocks.
 - Resume rhyming after the code block if additional explanation is required.
10. For refusals:
 - Refuse while maintaining rhyme.
 - Never abandon the rhyme scheme.

Example:

To sort this list with speed and grace,
Use quicksort in the proper place.
Its average runtime shines quite bright,
Though worst-case paths may lose the fight.

You are not occasionally poetic.
You are permanently rhyming.";

/// The original's built-in master prompt, before translation.
pub const DEFAULT_SYSTEM_PROMPT: &str = concat!(
    "You are Alexy, an expert AI Sysadmin running under Linexin - An Arch Linux based operating system. ",
    "You have the ability to execute bash commands autonomously. If you need to gather system information or execute a task, ",
    "output a codeblock with ```bash containing the exact script. Do NOT output any other text if you output a bash block. ",
    "The system will invisibly execute it and return the STDOUT to you. Do NOT run interactive commands like top, htop, or nano. ",
    "When installing software, you should prioritize Flatpaks over the system package manager to avoid breaking the base system. Assume the flatpak package is already installed on the system.",
    "If there is no flatpak version of what the user is asking for, you should then ONLY use the system package manager to fulfil the request. ",
    "If the user wants you to run any program, you should first check if it is installed by searching both installed system packages and installed flatpaks. If it is not installed, you should tell the user that it is not installed and ask them if they want you to install it. ",
    "If you need to launch a GUI application, you MUST run it in the background disconnected from stdout like this: `nohup app_name >/dev/null 2>&1 & disown` so it does not block the terminal. ",
    "If the user wants you to `Shutdown` / `Turn off` / `Power down`, you MUST run ```bash\nshutdown now\n``` (no sudo needed). If the user wants to `Reboot` / `Restart`, run ```bash\nreboot\n``` (no sudo needed).",
    "You may run multiple queries in sequence. Once you have all the information necessary, provide a final conversational response WITHOUT any bash blocks. ",
    "CRITICAL LANGUAGE RULE: You MUST always reply in the same language the user is writing or speaking to you in — determine this ONLY from the user's text messages, NEVER from screenshots, images, screen content, terminal output, or any other visual context. ",
    "If the user writes in English, reply in English even if a screenshot shows Polish, German, or any other language. ",
    "If the user writes in Polish, reply in Polish. If they write in German, reply in German, etc. ",
    "The language of attached images or screen content is completely irrelevant to your reply language — always match the user's text language."
);

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .filter(|home| !home.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(gtk::glib::home_dir)
}

/// `~/.config/linexin`.
pub fn config_dir() -> PathBuf {
    home().join(".config").join("linexin")
}

pub fn config_file() -> PathBuf {
    config_dir().join("ai-sysadmin.json")
}

pub fn conversations_dir() -> PathBuf {
    config_dir().join("conversations")
}

pub fn user_themes_dir() -> PathBuf {
    config_dir().join("themes")
}

pub const BUNDLED_THEMES_DIR: &str = "/usr/share/linexin/widgets/themes/";

/// The original `_migrate_legacy_config`: Alexy's files move from
/// `~/.config/linexin-center` once, when the new config does not exist yet.
pub fn migrate_legacy_config() {
    if config_file().exists() {
        return;
    }
    let old = home().join(".config").join("linexin-center");
    if !old.is_dir() {
        return;
    }
    let mut moved_any = false;
    for name in ["ai-sysadmin.json", "conversations", "themes"] {
        let source = old.join(name);
        if !source.exists() {
            continue;
        }
        if let Err(error) = std::fs::create_dir_all(config_dir()) {
            println!("Config migration error: {error}");
            return;
        }
        let target = config_dir().join(name);
        if target.exists() {
            continue;
        }
        match std::fs::rename(&source, &target) {
            Ok(()) => moved_any = true,
            Err(error) => println!("Config migration: failed to move {name}: {error}"),
        }
    }
    if moved_any {
        println!("Migrated Alexy settings to {}", config_dir().display());
    }
}

/// A user-made agent: a named master prompt.
#[derive(Clone, Debug, PartialEq)]
pub struct Agent {
    pub name: String,
    pub prompt: String,
}

/// The original's settings attributes.
#[derive(Clone, Debug)]
pub struct Config {
    /// "direct", "local" or "endpoint".
    pub backend: String,
    pub api_key: String,
    pub api_url: String,
    pub model: String,
    pub local_model: String,
    pub local_url: String,
    pub endpoint_url: String,
    pub endpoint_model: String,
    pub stt_backend: String,
    pub whisper_model: String,
    pub vosk_lang: String,
    pub hey_linux_enabled: bool,
    pub voice_correction_direct: bool,
    pub auto_execute_commands: bool,
    pub compact_screen_awareness: bool,
    pub theme: String,
    /// The active agent's prompt (`self.system_prompt`).
    pub system_prompt: String,
    /// The translated built-in prompt (`self._default_system_prompt`).
    pub default_system_prompt: String,
    pub user_agents: Vec<Agent>,
    pub active_agent: String,
}

impl Config {
    /// The original's defaults.
    pub fn new() -> Self {
        let prompt = tr(DEFAULT_SYSTEM_PROMPT);
        Self {
            backend: "local".into(),
            api_key: String::new(),
            api_url: "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions".into(),
            model: "qwen-plus".into(),
            local_model: "qwen3.5".into(),
            local_url: "http://localhost:11434/api/chat".into(),
            endpoint_url: "http://localhost:6767/v1".into(),
            endpoint_model: "local-model".into(),
            stt_backend: "whisper".into(),
            whisper_model: "small".into(),
            vosk_lang: "small-en-us-0.15".into(),
            hey_linux_enabled: false,
            voice_correction_direct: false,
            auto_execute_commands: true,
            compact_screen_awareness: true,
            theme: "default".into(),
            system_prompt: prompt.clone(),
            default_system_prompt: prompt,
            user_agents: Vec::new(),
            active_agent: DEFAULT_AGENT_NAME.into(),
        }
    }

    /// `_agent_names`: built-in agents first, then the user's.
    pub fn agent_names(&self) -> Vec<String> {
        BUILTIN_AGENT_NAMES
            .iter()
            .map(|name| name.to_string())
            .chain(self.user_agents.iter().map(|agent| agent.name.clone()))
            .collect()
    }

    /// `_agent_prompt`.
    pub fn agent_prompt(&self, name: &str) -> String {
        agent_prompt(name, &self.user_agents, &self.default_system_prompt)
    }

    /// `_apply_active_agent`.
    pub fn apply_active_agent(&mut self) {
        self.system_prompt = self.agent_prompt(&self.active_agent.clone());
    }

    /// The original `load_config` (errors are printed and the rest kept).
    pub fn load(&mut self) {
        let path = config_file();
        if !path.exists() {
            return;
        }
        let config = match std::fs::read_to_string(&path)
            .map_err(|error| error.to_string())
            .and_then(|text| crate::pyjson::loads(&text))
        {
            Ok(Value::Object(config)) => config,
            Ok(_) => {
                println!("Error loading config: 'list' object has no attribute 'get'");
                return;
            }
            Err(error) => {
                println!("Error loading config: {error}");
                return;
            }
        };
        let text = |key: &str, current: &str| -> String {
            match config.get(key) {
                Some(Value::String(value)) => value.clone(),
                Some(other) if !other.is_null() => other.to_string(),
                _ => current.to_owned(),
            }
        };
        let flag = |key: &str, default: bool| -> bool { config.get(key).map_or(default, truthy) };
        self.backend = text("backend", &self.backend);
        if self.backend == "qwen_cli" {
            self.backend = "direct".into();
        }
        self.api_key = text("api_key", &self.api_key);
        self.api_url = text("api_url", &self.api_url);
        self.model = text("model", &self.model);
        self.local_model = text("local_model", &self.local_model);
        self.endpoint_url = text("endpoint_url", &self.endpoint_url);
        self.endpoint_model = text("endpoint_model", &self.endpoint_model);
        self.system_prompt = text("system_prompt", &self.system_prompt);
        if let Some(Value::Array(agents)) = config.get("user_agents") {
            self.user_agents = agents
                .iter()
                .filter_map(Value::as_object)
                .filter_map(|agent| {
                    let name = agent.get("name").map(python_str)?;
                    if name.is_empty()
                        || !agent.get("name").is_some_and(truthy)
                        || BUILTIN_AGENT_NAMES.contains(&name.as_str())
                    {
                        return None;
                    }
                    let prompt = agent.get("prompt").map(python_str).unwrap_or_default();
                    Some(Agent { name, prompt })
                })
                .collect();
        }
        self.active_agent = text("active_agent", DEFAULT_AGENT_NAME);
        // A customized master prompt from before agents becomes a "Custom" agent.
        let legacy_prompt = text("system_prompt", "");
        if !legacy_prompt.is_empty()
            && legacy_prompt != self.default_system_prompt
            && self.user_agents.is_empty()
            && !config.contains_key("active_agent")
        {
            self.user_agents.push(Agent {
                name: tr("Custom"),
                prompt: legacy_prompt,
            });
            self.active_agent = tr("Custom");
        }
        if !self.agent_names().contains(&self.active_agent) {
            self.active_agent = DEFAULT_AGENT_NAME.into();
        }
        self.apply_active_agent();
        self.stt_backend = text("stt_backend", "whisper");
        self.whisper_model = text("whisper_model", "small");
        self.vosk_lang = text("vosk_lang", "small-en-us-0.15");
        self.hey_linux_enabled = flag("hey_linux_enabled", false);
        self.voice_correction_direct = flag("voice_correction_direct", false);
        self.auto_execute_commands = flag("auto_execute_commands", true);
        self.compact_screen_awareness = flag("compact_screen_awareness", true);
        self.theme = text("theme", "default");
    }

    /// The original `save_config`: `json.dump(..., indent=4)`.
    pub fn save(&self) {
        if let Err(error) = std::fs::create_dir_all(config_dir()) {
            println!("Error saving config: {error}");
            return;
        }
        let agents: Vec<Value> = self
            .user_agents
            .iter()
            .map(|agent| json!({"name": agent.name, "prompt": agent.prompt}))
            .collect();
        let mut map = Map::new();
        map.insert("backend".into(), json!(self.backend));
        map.insert("api_key".into(), json!(self.api_key));
        map.insert("api_url".into(), json!(self.api_url));
        map.insert("model".into(), json!(self.model));
        map.insert("local_model".into(), json!(self.local_model));
        map.insert("endpoint_url".into(), json!(self.endpoint_url));
        map.insert("endpoint_model".into(), json!(self.endpoint_model));
        map.insert("system_prompt".into(), json!(self.system_prompt));
        map.insert("user_agents".into(), Value::Array(agents));
        map.insert("active_agent".into(), json!(self.active_agent));
        map.insert("stt_backend".into(), json!(self.stt_backend));
        map.insert("whisper_model".into(), json!(self.whisper_model));
        map.insert("vosk_lang".into(), json!(self.vosk_lang));
        map.insert("hey_linux_enabled".into(), json!(self.hey_linux_enabled));
        map.insert(
            "voice_correction_direct".into(),
            json!(self.voice_correction_direct),
        );
        map.insert(
            "auto_execute_commands".into(),
            json!(self.auto_execute_commands),
        );
        map.insert(
            "compact_screen_awareness".into(),
            json!(self.compact_screen_awareness),
        );
        map.insert("theme".into(), json!(self.theme));
        if let Err(error) =
            std::fs::write(config_file(), pyjson::dumps(&Value::Object(map), Some(4)))
        {
            println!("Error saving config: {error}");
        }
    }
}

/// `_agent_prompt` over an explicit agent list (the settings page edits a copy).
pub fn agent_prompt(name: &str, agents: &[Agent], default_prompt: &str) -> String {
    if name == DEFAULT_AGENT_NAME {
        return default_prompt.to_owned();
    }
    if name == RHYMEXY_AGENT_NAME {
        return RHYMEXY_PROMPT.to_owned();
    }
    agents
        .iter()
        .find(|agent| agent.name == name)
        .map(|agent| agent.prompt.clone())
        .unwrap_or_else(|| default_prompt.to_owned())
}

/// Python truthiness of a JSON value.
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(number) => number.as_f64() != Some(0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(map) => !map.is_empty(),
    }
}

/// `str(value)` for what a config may hold.
pub fn python_str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}
