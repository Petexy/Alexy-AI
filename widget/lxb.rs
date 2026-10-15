//! The LineXinBar page: Alexy in LineXinBar's own language, driven by
//! controller, keyboard and pointer, with what the GTK page does.
//!
//! The chat: the conversation (replies streaming in, their reasoning to
//! open), then a field to ask in (LineXinBar's on-screen keyboard rises over
//! it with a controller in hand), images attached from the file chooser,
//! Listen (Whisper or Vosk, as the GTK page), Screen Awareness, the agent,
//! a new conversation, the saved ones (filtered by backend, renamed,
//! deleted) and Settings: the assistant (its switches, the backend, the
//! online API's key, address and model, the endpoint, Ollama's models pulled
//! and removed, Ollama installed or removed), the agents (created, renamed,
//! their prompts edited, deleted) and speech (the engine, Whisper's model,
//! Vosk's language and python-vosk, "Hey Alexy", voice correction). The GTK
//! page's themes dress its own bubbles and are not offered here.
//!
//! Replies that answer the voice are read aloud. Commands the model runs
//! are asked about with the shell's dialog where auto-execution is off, and
//! privileged ones go through pkexec, whose password the session's polkit
//! agent asks for (on LineXinBar, the shell's own panel). Where the toolkit
//! cannot take text yet, the page is the voice assistant it was, and the
//! settings that need typing show what they are set to.

use crate::config::{self, Agent, BUILTIN_AGENT_NAMES, Config, DEFAULT_AGENT_NAME};
use crate::conversations::{self, Listed};
use crate::settings::{
    self, INSTALL_OLLAMA, INSTALL_VOSK, POPULAR_NAMES, REAL_POPULAR_NAMES, UNINSTALL_OLLAMA,
    WHISPER_MODELS,
};
use crate::tts::{self, Speech};
use crate::ui::tr;
use crate::voice::{self, Heard, Voice, VoskHeard, VoskStart};
use crate::worker::{Settings, Shared, ToUi, Turn};
use base64::Engine;
use futures_channel::mpsc::UnboundedReceiver;
use gtk::glib;
use linexin_lxb::{
    Action, Emblem, Flow, FlowFrame, Metric, Page, PickerSelection, SidePanel, WidgetFrame, guard,
    icon_file, page_result,
};
use linexin_plugin_api::{self as api, LXB_PAGE_STAY, LxbContextV1, LxbFrameV1};
use regex::Regex;
use serde_json::{Value, json};
use std::ffi::c_void;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::ptr;
use std::sync::atomic::Ordering;
use std::sync::mpsc::{self, Receiver, TryRecvError};
use std::sync::{Arc, LazyLock, Mutex};

/// The page's mark (the column's entry wears it where pictures are not
/// drawn).
const MARK: &str = "setting-microphone";
/// Alexy's own icon, at the head of the page.
const ICON: &str = "/usr/share/icons/github.petexy.alexy.svg";
/// The GTK settings' Whisper model sizes, in `WHISPER_MODELS`' order.
const WHISPER_LABELS: [&str; 4] = [
    "Tiny (~39 MB, fastest)",
    "Base (~74 MB)",
    "Small (~461 MB, recommended)",
    "Medium (~1.5 GB, most accurate)",
];
/// The backends, as the GTK settings name them, in their order.
const BACKENDS: [(&str, &str); 3] = [
    ("direct", "Direct API (Online)"),
    ("local", "Local AI (Ollama)"),
    ("endpoint", "Local AI (Endpoint)"),
];

/// One message as the page shows it.
struct Said {
    user: bool,
    text: String,
    /// The model's reasoning before the reply, and whether it is open.
    reasoning: String,
    open: bool,
    /// Images sent with it.
    images: usize,
}

impl Said {
    fn reply(text: String) -> Self {
        Self {
            user: false,
            text,
            reasoning: String::new(),
            open: false,
            images: 0,
        }
    }
}

/// Which page of Alexy's is shown.
#[derive(Clone, Debug, PartialEq, Eq)]
enum Screen {
    Chat,
    Conversations,
    /// Renaming the saved conversation with this id.
    RenameConversation(String),
    Settings,
    Assistant,
    Agents,
    Speech,
    /// Naming a new agent (`None`) or renaming this one.
    AgentName(Option<String>),
    /// Editing this agent's prompt.
    Prompt(String),
}

impl Screen {
    /// Where Back goes from here.
    fn back(&self) -> Option<Screen> {
        match self {
            Screen::Chat => None,
            Screen::Conversations | Screen::Settings => Some(Screen::Chat),
            Screen::RenameConversation(_) => Some(Screen::Conversations),
            Screen::Assistant | Screen::Agents | Screen::Speech => Some(Screen::Settings),
            Screen::AgentName(_) | Screen::Prompt(_) => Some(Screen::Agents),
        }
    }
}

/// A question of this page's own on screen.
enum Question {
    /// Run a command the model wants (auto-execution off): the worker
    /// waits for the answer.
    Safety(mpsc::Sender<bool>),
    /// Ollama is missing: install it?
    InstallOllama,
    /// Something to read.
    Note,
}

/// A menu of this page's own on screen.
enum Menu {
    Agents(Vec<String>),
    /// Rename or delete a saved conversation.
    Conversation(Listed),
    ConversationFilter(Vec<String>),
    Backend,
    Models(Vec<String>),
    Popular,
    SpeechBackend,
    WhisperSize,
    VoskLanguage(Vec<&'static str>),
}

/// Something long the page runs itself, reporting lines and its end.
enum JobKind {
    InstallOllama,
    UninstallOllama,
    /// A model pulled: for a turn waiting on it, or from the settings.
    Pull {
        model: String,
        resume: bool,
    },
    RemoveModel(String),
    Whisper,
    VoskModel,
    InstallVosk,
    /// Piper or its voice, then this line speaks.
    Speech(String),
}

enum JobEvent {
    Line(String),
    Done(bool),
}

struct Job {
    kind: JobKind,
    events: Receiver<JobEvent>,
}

/// An image to send with the next message: its type, its bytes in base64,
/// and its file's name.
struct Attached {
    mime: String,
    data: String,
    name: String,
}

struct State {
    /// The chat and, in a wide window, the panel beside it (the controls
    /// and the recent conversations, as the GTK page's sidebar); the other
    /// pages share `other`.
    panel: SidePanel,
    /// The saved conversations, read again when they change.
    saved: Option<Vec<Listed>>,
    other: Flow,
    screen: Screen,
    config: Config,
    shared: Arc<Shared>,
    conversation_id: String,
    conv_created: Option<String>,
    conv_agent: String,
    said: Vec<Said>,
    /// The reply arriving: its content and reasoning so far.
    stream: Option<(String, String)>,
    /// What is happening, under the conversation.
    status: Option<String>,
    /// A turn is running (the original's `llm_processing`).
    busy: bool,
    events: Option<UnboundedReceiver<ToUi>>,
    /// What is being typed to Alexy, and the images going with it.
    draft: String,
    attached: Vec<Attached>,
    /// Choosing an image in the file chooser.
    picking: bool,
    /// Voice-heard words being corrected (Direct API's voice correction)
    /// before they are sent, with the images and whether the screen was
    /// captured.
    correcting: Option<Receiver<String>>,
    voice: Voice,
    whisper_available: bool,
    vosk_available: bool,
    heard: Option<Receiver<Heard>>,
    loading: Option<Receiver<Result<(), String>>>,
    vosk_starting: Option<Receiver<VoskStart>>,
    vosk_heard: Option<Receiver<VoskHeard>>,
    job: Option<Job>,
    tts: Option<Child>,
    asking: Option<Question>,
    menu: Option<Menu>,
    /// Screen Awareness: a screenshot goes with what is sent.
    screen_awareness: bool,
    /// `--voice` (or SIGUSR1): listen on the next frame.
    listen_first: bool,
    /// The conversations listed, by the backend filter.
    conv_filter: Option<String>,
    /// What a name or a prompt is being edited into.
    editing: String,
    /// Ollama's downloaded models (`(name, size)`), once listed.
    models: Option<Vec<(String, String)>>,
    models_loading: Option<Receiver<Vec<(String, String)>>>,
    /// The model the settings would pull.
    pull_name: String,
    /// The endpoint's address as the Assistant page found it.
    endpoint_before: String,
}

pub unsafe extern "C" fn create(context: *const LxbContextV1) -> *mut c_void {
    guard(ptr::null_mut(), || {
        // SAFETY: the pointer the host passed to `lxb_create`.
        let Some(context) = (unsafe { LxbContextV1::read(context) }) else {
            return ptr::null_mut();
        };
        config::migrate_legacy_config();
        let mut config = Config::new();
        config.load();
        let history = vec![json!({"role": "system", "content": config.system_prompt})];
        let voice_autostart = context.voice_autostart();
        let mut state = State {
            panel: SidePanel::new(),
            saved: None,
            other: Flow::new(),
            screen: Screen::Chat,
            conv_agent: config.active_agent.clone(),
            endpoint_before: config.endpoint_url.clone(),
            config,
            shared: Arc::new(Shared {
                history: Mutex::new(history),
                abort: Default::default(),
                speak_next: Default::default(),
            }),
            conversation_id: glib::uuid_string_random().to_string(),
            conv_created: None,
            said: vec![greeting()],
            stream: None,
            status: None,
            busy: false,
            events: None,
            draft: String::new(),
            attached: Vec::new(),
            picking: false,
            correcting: None,
            voice: Voice::default(),
            whisper_available: voice::python_has("whisper"),
            vosk_available: voice::python_has("vosk"),
            heard: None,
            loading: None,
            vosk_starting: None,
            vosk_heard: None,
            job: None,
            tts: None,
            asking: None,
            menu: None,
            // As on the GTK page, a voice launch always sends the screen too.
            screen_awareness: voice_autostart,
            listen_first: voice_autostart,
            conv_filter: None,
            editing: String::new(),
            models: None,
            models_loading: None,
            pull_name: REAL_POPULAR_NAMES[0].to_owned(),
        };
        if let Some(id) = context.conversation_id() {
            state.load_conversation(&id);
        }
        Box::into_raw(Box::new(state)).cast()
    })
}

pub unsafe extern "C" fn page(state: *mut c_void, frame: *const LxbFrameV1) -> u32 {
    guard(LXB_PAGE_STAY, || {
        // SAFETY: the state from `create`, alive for the call.
        let Some(state) = (unsafe { state.cast::<State>().as_mut() }) else {
            return LXB_PAGE_STAY;
        };
        // SAFETY: the host's frame, read for this call only.
        let Some(frame) = (unsafe { WidgetFrame::read(frame) }) else {
            return LXB_PAGE_STAY;
        };
        state.draw(&frame)
    })
}

pub unsafe extern "C" fn destroy(state: *mut c_void) {
    guard((), || {
        if state.is_null() {
            return;
        }
        // SAFETY: the state from `create`, released once.
        let mut state = unsafe { Box::from_raw(state.cast::<State>()) };
        state.voice.stop();
        state.shared.abort.store(true, Ordering::SeqCst);
        state.stop_speaking();
        state.save_conversation();
        crate::screen::cleanup_tmp();
    })
}

/// SIGUSR1 (the wake-word daemon): listen again.
pub unsafe extern "C" fn reactivate_voice(state: *mut c_void) {
    guard((), || {
        // SAFETY: the state from `create`, between frames.
        let Some(state) = (unsafe { state.cast::<State>().as_mut() }) else {
            return;
        };
        if state.listening() {
            state.voice.stop();
            state.heard = None;
            state.loading = None;
            state.vosk_starting = None;
            state.vosk_heard = None;
        }
        state.screen = Screen::Chat;
        state.listen_first = true;
    })
}

fn greeting() -> Said {
    Said::reply(tr("Hello! I am Alexy. How can I help you today?"))
}

/// What a control pressed on the chat asks for.
enum Press {
    Listen,
    Stop,
    Attach,
    Unattach(usize),
    Screen,
    Agent,
    New,
    Conversations,
    Settings,
    Reasoning(usize),
    /// A recent conversation in the panel.
    Recent(usize),
}

/// How many recent conversations the panel lists.
const RECENT: usize = 12;

/// The chat's controls, as they stand this frame.
struct Controls {
    /// Listening, thinking or speaking: Stop rather than Listen.
    stoppable: bool,
    screen: bool,
    agent: String,
}

impl Controls {
    /// Listen (or Stop), Attach an image, Screen Awareness, the agent, a new
    /// conversation, the saved ones and Settings; the one pressed.
    fn draw(&self, flow: &mut FlowFrame) -> Option<Press> {
        let mut pressed = None;
        if self.stoppable {
            if flow.entry("key-escape", &tr("Stop"), None) {
                pressed = Some(Press::Stop);
            }
        } else if flow.entry("setting-microphone", &tr("Listen"), None) {
            pressed = Some(Press::Listen);
        }
        if flow.entry("add", &tr("Attach an image"), None) {
            pressed = Some(Press::Attach);
        }
        if flow.switch("screenshot", &tr("Screen Awareness"), self.screen) {
            pressed = Some(Press::Screen);
        }
        if flow.entry(
            "setting-account-type",
            &tr("Active Agent"),
            Some(&self.agent),
        ) {
            pressed = Some(Press::Agent);
        }
        if flow.entry("new-folder", &tr("Start a new conversation"), None) {
            pressed = Some(Press::New);
        }
        if flow.entry("file-folder", &tr("Browse saved conversations"), None) {
            pressed = Some(Press::Conversations);
        }
        if flow.entry("setting-system", &tr("Settings"), None) {
            pressed = Some(Press::Settings);
        }
        pressed
    }
}

impl State {
    fn listening(&self) -> bool {
        self.heard.is_some()
            || self.loading.is_some()
            || self.vosk_starting.is_some()
            || self.vosk_heard.is_some()
    }

    fn speaking(&mut self) -> bool {
        match &mut self.tts {
            Some(child) => match child.try_wait() {
                Ok(None) => true,
                _ => {
                    self.tts = None;
                    false
                }
            },
            None => false,
        }
    }

    fn draw(&mut self, frame: &WidgetFrame) -> u32 {
        let page = &frame.page;
        self.collect(page);
        self.take_answers(page);
        if self.listen_first {
            self.listen_first = false;
            self.listen();
        }
        let speaking = self.speaking();
        if self.busy
            || self.listening()
            || self.job.is_some()
            || speaking
            || self.correcting.is_some()
            || self.models_loading.is_some()
        {
            page.redraw_within(0.1);
        }
        match self.screen.clone() {
            Screen::Chat => self.chat(frame, speaking),
            screen => {
                let input = if frame.focused {
                    self.other.input(page, &frame.actions)
                } else {
                    Default::default()
                };
                let menu = input.unhandled.contains(&Action::Menu);
                let back = match &screen {
                    Screen::Conversations => self.conversations_page(frame, menu),
                    Screen::RenameConversation(id) => self.rename_conversation_page(frame, id),
                    Screen::Settings => self.settings_page(frame),
                    Screen::Assistant => self.assistant_page(frame),
                    Screen::Agents => self.agents_page(frame),
                    Screen::Speech => self.speech_page(frame),
                    Screen::AgentName(old) => self.agent_name_page(frame, old.clone()),
                    Screen::Prompt(name) => self.prompt_page(frame, name),
                    Screen::Chat => false,
                };
                if (back || input.leave.is_some())
                    && self.screen == screen
                    && let Some(parent) = screen.back()
                {
                    self.go(parent);
                }
                LXB_PAGE_STAY
            }
        }
    }

    /// The saved conversations, newest first in each agent's group.
    fn saved(&mut self) -> &[Listed] {
        self.saved.get_or_insert_with(|| conversations::list(None))
    }

    /// Show another of the page's pages.
    fn go(&mut self, screen: Screen) {
        if self.screen == Screen::Assistant && screen != Screen::Assistant {
            self.leave_assistant();
        }
        match &screen {
            Screen::Assistant => {
                self.endpoint_before = self.config.endpoint_url.clone();
                self.list_models();
            }
            Screen::RenameConversation(id) => {
                self.editing = self
                    .saved()
                    .iter()
                    .find(|listed| listed.id == *id)
                    .cloned()
                    .map(|listed| listed.title)
                    .unwrap_or_default();
            }
            Screen::AgentName(Some(name)) => self.editing = name.clone(),
            Screen::AgentName(None) => self.editing.clear(),
            Screen::Prompt(name) => {
                self.editing = self
                    .config
                    .user_agents
                    .iter()
                    .find(|agent| agent.name == *name)
                    .map(|agent| agent.prompt.clone())
                    .unwrap_or_default();
            }
            _ => {}
        }
        self.screen = screen;
        self.other.reset();
    }

    fn subtitle(&self) -> String {
        let config = &self.config;
        match config.backend.as_str() {
            "direct" => tr("Online API: {}").replacen("{}", &config.model, 1),
            "endpoint" => {
                let model = if config.endpoint_model.is_empty() {
                    tr("auto-detected")
                } else {
                    config.endpoint_model.clone()
                };
                tr("Local AI: {}").replacen("{}", &model, 1)
            }
            _ => tr("Local AI: {}").replacen("{}", &config.local_model, 1),
        }
    }

    // -- the chat ---------------------------------------------------------------------

    fn chat(&mut self, frame: &WidgetFrame, speaking: bool) -> u32 {
        let page = &frame.page;
        let side_width = SidePanel::width(page, frame.rect);
        let (main_rect, side_rect) =
            SidePanel::split(page, frame.rect, side_width, page.scaled(620.0));
        let wide = side_rect.is_some();
        let (main_input, side_input) = self.panel.input(page, &frame.actions, frame.focused, wide);
        let (main_lit, side_lit) = self.panel.lit(frame.focused, wide);
        let mut pressed = None;
        let agent = self.conv_agent.clone();
        let subtitle = self.subtitle();
        let controls = Controls {
            stoppable: self.listening() || self.busy || speaking || self.correcting.is_some(),
            screen: self.screen_awareness,
            agent: self.config.active_agent.clone(),
        };
        let typing = page.takes_typing();
        let recent: Vec<Listed> = if wide {
            let mut saved = self.saved().to_vec();
            saved.truncate(RECENT);
            saved
        } else {
            Vec::new()
        };
        let icon = icon_file(ICON);
        let (you, image, reasoning) = (tr("You"), tr("Image"), tr("Reasoning"));
        let mut flow = self.panel.main.begin(page, main_rect, main_lit);
        flow.head(Emblem::with_picture(icon.as_deref(), MARK), &tr("Alexy AI"));
        flow.note(&subtitle);
        flow.gap();
        for (index, said) in self.said.iter().enumerate() {
            flow.note(if said.user { &you } else { &agent });
            if !said.reasoning.trim().is_empty() {
                if flow.entry("setting-info", &reasoning, None) {
                    pressed = Some(Press::Reasoning(index));
                }
                if said.open {
                    flow.note(said.reasoning.trim());
                }
            }
            for _ in 0..said.images {
                flow.note(&format!("\u{1f5bc} {image}"));
            }
            if !said.text.is_empty() {
                flow.text(&said.text);
            }
        }
        if let Some((content, thinking)) = &self.stream {
            flow.note(&agent);
            let content = visible(content);
            if !content.trim().is_empty() {
                flow.text(&plain(&content));
            } else if !thinking.trim().is_empty() {
                flow.text(&tr("Thinking..."));
            }
        }
        if let Some(status) = &self.status {
            flow.note(status);
        }
        if wide {
            // The field at the chat's foot, as the GTK page's entry, under a
            // conversation too short to reach it.
            let row = page.metric(Metric::RowHeight);
            let images = self.attached.len() as f32;
            flow.push_to_end(page.metric(Metric::Gap) * 1.5 + row * (1.0 + images) + 1.0);
        }
        flow.rule();
        let field = if typing {
            flow.field("", &tr("Ask a question..."), &mut self.draft, false)
        } else {
            Default::default()
        };
        for (index, attached) in self.attached.iter().enumerate() {
            if flow.entry("file-page", &image, Some(&attached.name)) {
                pressed = Some(Press::Unattach(index));
            }
        }
        if !wide {
            pressed = controls.draw(&mut flow).or(pressed);
        }
        flow.finish();
        if let Some(side) = side_rect {
            // Level with the conversation, under the page's heading.
            let pinned = self.panel.main.pinned();
            let side = [
                side[0],
                side[1] + pinned,
                side[2],
                (side[3] - pinned).max(0.0),
            ];
            let mut flow = self.panel.side.begin(page, side, side_lit);
            pressed = controls.draw(&mut flow).or(pressed);
            if !recent.is_empty() {
                flow.gap();
                flow.heading(&tr("Browse saved conversations"));
                for (index, conversation) in recent.iter().enumerate() {
                    let mark = if conversation.id == self.conversation_id {
                        "chosen"
                    } else {
                        ""
                    };
                    if flow.detailed(
                        mark,
                        &conversation.title,
                        &conversations::display_time(&conversation.updated),
                        None,
                    ) {
                        pressed = Some(Press::Recent(index));
                    }
                }
            }
            flow.finish();
        }
        let leave = self.panel.settle(&main_input, &side_input, wide);

        if field.submitted {
            let text = std::mem::take(&mut self.draft);
            self.send(&text, false);
        }
        match pressed {
            Some(Press::Listen) => self.listen(),
            Some(Press::Stop) => self.stop(),
            Some(Press::Screen) => {
                self.screen_awareness = !self.screen_awareness;
                println!(
                    "[Screen Awareness] _on_screen_toggle called: active={}",
                    crate::ui::python_bool(self.screen_awareness)
                );
            }
            Some(Press::Reasoning(index)) => {
                if let Some(said) = self.said.get_mut(index) {
                    said.open = !said.open;
                }
            }
            Some(Press::Unattach(index)) => {
                if index < self.attached.len() {
                    self.attached.remove(index);
                }
            }
            Some(Press::Attach) => {
                if page.pick(PickerSelection::Image, home()) {
                    self.picking = true;
                }
            }
            Some(Press::Conversations) => self.go(Screen::Conversations),
            Some(Press::Settings) => self.go(Screen::Settings),
            // What changes the conversation waits while a reply is coming.
            Some(_) if self.busy => {}
            Some(Press::Agent) => self.offer_agents(page),
            Some(Press::New) => self.new_conversation(),
            Some(Press::Recent(index)) => {
                if let Some(conversation) = recent.get(index) {
                    let id = conversation.id.clone();
                    self.load_conversation(&id);
                }
            }
            None => {}
        }
        page_result(leave)
    }

    fn offer_agents(&mut self, page: &Page) {
        let names = self.config.agent_names();
        let marked = names
            .iter()
            .position(|name| *name == self.config.active_agent);
        let commands: Vec<&str> = names.iter().map(String::as_str).collect();
        page.menu_marked(Some(&tr("Active Agent")), &commands, marked);
        self.menu = Some(Menu::Agents(names));
    }

    // -- the saved conversations ----------------------------------------------------

    /// The GTK page's conversations panel: grouped by agent, filtered by
    /// backend where there are several, the open one marked; a row loads,
    /// its Options rename or delete it.
    fn conversations_page(&mut self, frame: &WidgetFrame, menu: bool) -> bool {
        let page = &frame.page;
        let all = self.saved().to_vec();
        let mut available: Vec<String> = Vec::new();
        for listed in &all {
            if !available.contains(&listed.backend) {
                available.push(listed.backend.clone());
            }
        }
        if !available.contains(&self.config.backend) {
            available.push(self.config.backend.clone());
        }
        available.retain(|backend| BACKENDS.iter().any(|(key, _)| key == backend));
        let filter = match &self.conv_filter {
            Some(filter) if available.contains(filter) => filter.clone(),
            _ => self.config.backend.clone(),
        };
        let listed: Vec<Listed> = all
            .into_iter()
            .filter(|listed| available.len() <= 1 || listed.backend == filter)
            .collect();
        let (mut chosen, mut filter_menu, mut back) = (None, false, false);
        let mut flow = self.other.begin(page, frame.rect, frame.focused);
        flow.head("file-folder", &tr("Browse saved conversations"));
        let mut before = 0;
        if available.len() > 1 {
            if flow.entry("sort", &tr("Backend:"), Some(&backend_name(&filter))) {
                filter_menu = true;
            }
            before += 1;
        }
        if listed.is_empty() {
            flow.note(&tr("No saved conversations yet."));
        }
        let mut last_agent: Option<&str> = None;
        for (index, conversation) in listed.iter().enumerate() {
            let agent = if conversation.agent.is_empty() {
                DEFAULT_AGENT_NAME
            } else {
                conversation.agent.as_str()
            };
            if last_agent != Some(agent) {
                last_agent = Some(agent);
                flow.heading(agent);
            }
            let mark = if conversation.id == self.conversation_id {
                "chosen"
            } else {
                ""
            };
            if flow.detailed(
                mark,
                &conversation.title,
                &conversations::display_time(&conversation.updated),
                None,
            ) {
                chosen = Some(index);
            }
        }
        if flow.entry("arrow-left", &tr("Back"), None) {
            back = true;
        }
        flow.finish();
        let focused = self.other.focused().unwrap_or(0);
        if filter_menu {
            let labels: Vec<String> = available.iter().map(|key| backend_name(key)).collect();
            let commands: Vec<&str> = labels.iter().map(String::as_str).collect();
            let marked = available.iter().position(|key| *key == filter);
            page.menu_marked(Some(&tr("Backend:")), &commands, marked);
            self.menu = Some(Menu::ConversationFilter(available));
        } else if menu
            && let Some(conversation) = focused
                .checked_sub(before)
                .and_then(|index| listed.get(index))
        {
            page.menu(
                Some(&conversation.title),
                &[&tr("Rename conversation"), &tr("Delete conversation")],
            );
            self.menu = Some(Menu::Conversation(conversation.clone()));
        }
        if let Some(index) = chosen
            && let Some(conversation) = listed.get(index)
            && !self.busy
        {
            let id = conversation.id.clone();
            self.load_conversation(&id);
            self.go(Screen::Chat);
        }
        back
    }

    fn rename_conversation_page(&mut self, frame: &WidgetFrame, id: &str) -> bool {
        let page = &frame.page;
        let (mut save, mut cancel) = (false, false);
        let mut flow = self.other.begin(page, frame.rect, frame.focused);
        flow.head("rename", &tr("Rename conversation"));
        let field = flow.field("", &tr("Untitled"), &mut self.editing, false);
        if flow.entry("chosen", &tr("Rename"), None) {
            save = true;
        }
        if flow.entry("key-escape", &tr("Cancel"), None) {
            cancel = true;
        }
        flow.finish();
        if save || field.submitted {
            let title = conversations::python_strip(&self.editing).to_owned();
            if !title.is_empty() {
                conversations::rename(id, &title);
                self.saved = None;
            }
            self.go(Screen::Conversations);
        }
        cancel
    }

    // -- the settings ----------------------------------------------------------------

    fn settings_page(&mut self, frame: &WidgetFrame) -> bool {
        let page = &frame.page;
        let mut go = None;
        let mut back = false;
        let mut flow = self.other.begin(page, frame.rect, frame.focused);
        flow.head("setting-system", &tr("Settings"));
        let sections = [
            (
                "setting-connect",
                "Assistant",
                "Backend Type",
                Screen::Assistant,
            ),
            (
                "setting-account-type",
                "Agents",
                "Active Agent",
                Screen::Agents,
            ),
            (
                "setting-microphone",
                "Speech",
                "Voice-to-Text Engine",
                Screen::Speech,
            ),
        ];
        for (mark, title, note, screen) in sections {
            if flow.detailed(mark, &tr(title), &tr(note), None) {
                go = Some(screen);
            }
        }
        if flow.entry("arrow-left", &tr("Back"), None) {
            back = true;
        }
        flow.finish();
        if let Some(screen) = go {
            self.go(screen);
        }
        back
    }

    /// The GTK settings' Assistant page.
    fn assistant_page(&mut self, frame: &WidgetFrame) -> bool {
        let page = &frame.page;
        let typing = page.takes_typing();
        #[derive(PartialEq)]
        enum Do {
            AutoExecute,
            CompactScreen,
            Backend,
            Models,
            RemoveModel,
            Popular,
            Pull,
            InstallOllama,
            UninstallOllama,
            Back,
        }
        let mut done = None;
        let installed = crate::worker::ollama_installed();
        let backend = self.config.backend.clone();
        let mut flow = self.other.begin(page, frame.rect, frame.focused);
        flow.head("setting-connect", &tr("Assistant"));
        if let Some(status) = &self.status {
            flow.note(status);
        }
        flow.heading(&tr("General Settings"));
        if flow.switch(
            "",
            &tr("Auto-Execute Commands"),
            self.config.auto_execute_commands,
        ) {
            done = Some(Do::AutoExecute);
        }
        flow.note(&tr(
            "Allow AI to silently execute bash commands without prompting for permission first. Leave checked for a continuous experience.",
        ));
        if flow.switch(
            "",
            &tr("Compact Mode Screen Awareness"),
            self.config.compact_screen_awareness,
        ) {
            done = Some(Do::CompactScreen);
        }
        flow.note(&tr(
            "Automatically capture and send a screenshot to the AI when using compact voice mode (Hey Alexy).",
        ));
        flow.gap();
        flow.heading(&tr("Backend Type"));
        flow.note(&tr("Configure how Alexy connects to models."));
        if flow.entry(
            "setting-connect",
            &tr("Backend Type"),
            Some(&backend_label(&backend)),
        ) {
            done = Some(Do::Backend);
        }
        match backend.as_str() {
            "direct" => {
                flow.note(&tr(
                    "Uses urllib to connect directly to Qwen or OpenAI compatible APIs.",
                ));
                let config = &mut self.config;
                text_setting(&mut flow, typing, &tr("API Key"), &mut config.api_key, true);
                text_setting(
                    &mut flow,
                    typing,
                    &tr("API URL"),
                    &mut config.api_url,
                    false,
                );
                text_setting(&mut flow, typing, &tr("Model"), &mut config.model, false);
            }
            "endpoint" => {
                flow.note(&tr(
                    "Connects to a local OpenAI-compatible server via its endpoint URL (e.g. http://localhost:6767/v1). The model is detected automatically — no API key, model name or Ollama required.",
                ));
                text_setting(
                    &mut flow,
                    typing,
                    &tr("Endpoint URL"),
                    &mut self.config.endpoint_url,
                    false,
                );
            }
            _ => {
                flow.note(&tr(
                    "Uses Ollama daemon sequentially running on localhost:11434.",
                ));
                if installed {
                    flow.note(&tr("Ollama is installed"));
                    let model = if self.config.local_model.is_empty() {
                        tr("No Models Downloaded")
                    } else {
                        self.config.local_model.clone()
                    };
                    if flow.entry(
                        "category-system",
                        &tr("Select Downloaded Model"),
                        Some(&model),
                    ) {
                        done = Some(Do::Models);
                    }
                    if !self.config.local_model.is_empty()
                        && flow.entry(
                            "uninstall",
                            &tr("Remove Model"),
                            Some(&tr("Delete from Disk")),
                        )
                    {
                        done = Some(Do::RemoveModel);
                    }
                    flow.gap();
                    flow.heading(&tr("Download Model"));
                    let popular = REAL_POPULAR_NAMES
                        .iter()
                        .position(|name| *name == self.pull_name)
                        .map(|index| POPULAR_NAMES[index].to_owned())
                        .unwrap_or_default();
                    if flow.entry("search", &tr("Popular Models"), Some(&popular)) {
                        done = Some(Do::Popular);
                    }
                    text_setting(
                        &mut flow,
                        typing,
                        &tr("Model Name (e.g. qwen3.5)"),
                        &mut self.pull_name,
                        false,
                    );
                    let downloaded = self.models.as_ref().is_some_and(|models| {
                        models.iter().any(|(name, _)| *name == self.pull_name)
                    });
                    let note = downloaded.then(|| tr("Already Downloaded"));
                    if flow.entry("add", &tr("Start Download"), note.as_deref()) {
                        done = Some(Do::Pull);
                    }
                    flow.gap();
                    if flow.detailed(
                        "uninstall",
                        &tr("Uninstall Ollama"),
                        &tr("Remove the Ollama daemon and its dependencies from your system."),
                        None,
                    ) {
                        done = Some(Do::UninstallOllama);
                    }
                } else if flow.detailed(
                    "add",
                    &tr("Install Ollama"),
                    &tr("Install the Ollama daemon to use Local AI."),
                    None,
                ) {
                    done = Some(Do::InstallOllama);
                }
            }
        }
        flow.gap();
        if flow.entry("arrow-left", &tr("Back"), None) {
            done = Some(Do::Back);
        }
        flow.finish();
        match done {
            Some(Do::AutoExecute) => {
                self.config.auto_execute_commands = !self.config.auto_execute_commands;
                self.config.save();
            }
            Some(Do::CompactScreen) => {
                self.config.compact_screen_awareness = !self.config.compact_screen_awareness;
                self.config.save();
            }
            Some(Do::Backend) => {
                let labels: Vec<String> = BACKENDS.iter().map(|(_, label)| tr(label)).collect();
                let commands: Vec<&str> = labels.iter().map(String::as_str).collect();
                let marked = BACKENDS.iter().position(|(key, _)| *key == backend);
                page.menu_marked(Some(&tr("Backend Type")), &commands, marked);
                self.menu = Some(Menu::Backend);
            }
            Some(Do::Models) => {
                let models = self.models.clone().unwrap_or_default();
                if models.is_empty() {
                    page.ask(
                        &tr("No Models Downloaded"),
                        &tr("Download Model"),
                        &[&tr("OK")],
                    );
                    self.asking = Some(Question::Note);
                } else {
                    let labels: Vec<String> = models
                        .iter()
                        .map(|(name, size)| format!("{name} ({size})"))
                        .collect();
                    let commands: Vec<&str> = labels.iter().map(String::as_str).collect();
                    let marked = models
                        .iter()
                        .position(|(name, _)| *name == self.config.local_model);
                    page.menu_marked(Some(&tr("Select Downloaded Model")), &commands, marked);
                    self.menu = Some(Menu::Models(
                        models.into_iter().map(|(name, _)| name).collect(),
                    ));
                }
            }
            Some(Do::RemoveModel) => {
                let model = self.config.local_model.clone();
                self.status = Some(tr("Deleting model files..."));
                self.run_job(
                    JobKind::RemoveModel(model.clone()),
                    &format!("ollama rm {model}"),
                    false,
                );
            }
            Some(Do::Popular) => {
                let commands: Vec<&str> = POPULAR_NAMES.to_vec();
                let marked = REAL_POPULAR_NAMES
                    .iter()
                    .position(|name| *name == self.pull_name);
                page.menu_marked(Some(&tr("Popular Models")), &commands, marked);
                self.menu = Some(Menu::Popular);
            }
            Some(Do::Pull) => {
                let model = conversations::python_strip(&self.pull_name).to_owned();
                if model.is_empty() {
                    page.ask(
                        &tr("Download Model"),
                        &tr("Please enter a model name to pull."),
                        &[&tr("OK")],
                    );
                    self.asking = Some(Question::Note);
                } else {
                    self.status = Some(tr("Initiating download..."));
                    self.run_job(
                        JobKind::Pull {
                            model: model.clone(),
                            resume: false,
                        },
                        &format!("ollama pull {model}"),
                        false,
                    );
                }
            }
            Some(Do::InstallOllama) => {
                self.status = Some(tr("Downloading and installing Ollama daemon..."));
                self.run_job(JobKind::InstallOllama, INSTALL_OLLAMA, true);
            }
            Some(Do::UninstallOllama) => {
                self.status = Some(tr("Uninstalling Ollama"));
                self.run_job(JobKind::UninstallOllama, UNINSTALL_OLLAMA, true);
            }
            Some(Do::Back) => return true,
            None => {}
        }
        false
    }

    /// Leaving the Assistant page applies what was typed, as the GTK
    /// settings do when they close.
    fn leave_assistant(&mut self) {
        let endpoint = conversations::python_strip(&self.config.endpoint_url).to_owned();
        self.config.endpoint_url = endpoint.clone();
        if !endpoint.is_empty() && endpoint != self.endpoint_before {
            self.config.endpoint_model = String::new();
        } else if endpoint.is_empty() {
            self.config.endpoint_url = self.endpoint_before.clone();
        }
        self.config.save();
    }

    /// A backend chosen: the GTK settings start a new conversation for it.
    fn switch_backend(&mut self, backend: &str) {
        if backend == self.config.backend {
            return;
        }
        self.save_conversation();
        self.config.backend = backend.to_owned();
        self.config.save();
        self.conversation_id = glib::uuid_string_random().to_string();
        self.conv_created = None;
        self.reset_history();
        self.said = vec![
            greeting(),
            Said::reply(tr("Switched backend mode. New conversation started.")),
        ];
        self.status = None;
        if backend == "local" {
            self.list_models();
        }
    }

    /// The GTK settings' Agents page.
    fn agents_page(&mut self, frame: &WidgetFrame) -> bool {
        let page = &frame.page;
        enum Do {
            Active,
            Create,
            Rename,
            Prompt,
            Delete,
            Back,
        }
        let mut done = None;
        let active = self.config.active_agent.clone();
        let builtin = BUILTIN_AGENT_NAMES.contains(&active.as_str());
        let prompt = self.config.agent_prompt(&active);
        let mut flow = self.other.begin(page, frame.rect, frame.focused);
        flow.head("setting-account-type", &tr("Agents"));
        flow.note(&tr(
            "Agents are named master-prompt profiles. Select one to use for new conversations, or create your own. The built-in agents cannot be edited.",
        ));
        if flow.entry("setting-account-type", &tr("Active Agent"), Some(&active)) {
            done = Some(Do::Active);
        }
        flow.note(&tr("Applied to new conversations"));
        if flow.detailed(
            "add",
            &tr("Create New Agent"),
            &tr("Start from a copy of the default prompt"),
            None,
        ) {
            done = Some(Do::Create);
        }
        flow.gap();
        flow.heading(&active);
        if builtin {
            flow.note(&tr(
                "This is a built-in agent. Its prompt is shown for reference and cannot be edited.",
            ));
        } else {
            if flow.entry("rename", &tr("Rename Agent"), None) {
                done = Some(Do::Rename);
            }
            if flow.entry("setting-typed", &tr("Edit prompt"), None) {
                done = Some(Do::Prompt);
            }
            if flow.entry("uninstall", &tr("Delete Agent"), None) {
                done = Some(Do::Delete);
            }
        }
        for paragraph in prompt.split("\n\n").filter(|part| !part.trim().is_empty()) {
            flow.note(paragraph.trim());
        }
        flow.gap();
        if flow.entry("arrow-left", &tr("Back"), None) {
            done = Some(Do::Back);
        }
        flow.finish();
        match done {
            Some(Do::Active) => self.offer_agents(page),
            Some(Do::Create) => self.go(Screen::AgentName(None)),
            Some(Do::Rename) => self.go(Screen::AgentName(Some(active))),
            Some(Do::Prompt) => self.go(Screen::Prompt(active)),
            Some(Do::Delete) => {
                self.config.user_agents.retain(|agent| agent.name != active);
                self.choose_agent(DEFAULT_AGENT_NAME);
            }
            Some(Do::Back) => return true,
            None => {}
        }
        false
    }

    /// The agent `name` made the active one (and saved), as the GTK
    /// settings apply it: a conversation not begun yet takes it up.
    fn choose_agent(&mut self, name: &str) {
        self.config.active_agent = name.to_owned();
        if !self
            .config
            .agent_names()
            .contains(&self.config.active_agent)
        {
            self.config.active_agent = DEFAULT_AGENT_NAME.into();
        }
        self.config.apply_active_agent();
        self.config.save();
        let empty = self
            .shared
            .history
            .lock()
            .map(|history| history.len() <= 1)
            .unwrap_or(true);
        if empty {
            self.reset_history();
        }
    }

    /// Naming a new agent, or renaming one: a name no other agent has.
    fn agent_name_page(&mut self, frame: &WidgetFrame, old: Option<String>) -> bool {
        let page = &frame.page;
        let (title, body, confirm) = match &old {
            None => (
                tr("New Agent"),
                tr("Enter a name for the new agent."),
                tr("Create"),
            ),
            Some(_) => (
                tr("Rename Agent"),
                tr("Enter a new name for this agent."),
                tr("Rename"),
            ),
        };
        let (mut save, mut cancel) = (false, false);
        let mut flow = self.other.begin(page, frame.rect, frame.focused);
        flow.head("setting-account-type", &title);
        flow.text(&body);
        let field = flow.field("", &tr("Agent name"), &mut self.editing, false);
        if flow.entry("chosen", &confirm, None) {
            save = true;
        }
        if flow.entry("key-escape", &tr("Cancel"), None) {
            cancel = true;
        }
        flow.finish();
        if save || field.submitted {
            let name = conversations::python_strip(&self.editing).to_owned();
            let taken: Vec<String> = self
                .config
                .agent_names()
                .iter()
                .map(|existing| existing.to_lowercase())
                .filter(|existing| {
                    old.as_ref()
                        .is_none_or(|old| *existing != old.to_lowercase())
                })
                .collect();
            if name.is_empty() || taken.contains(&name.to_lowercase()) {
                return false;
            }
            match &old {
                None => self.config.user_agents.push(Agent {
                    name: name.clone(),
                    prompt: self.config.default_system_prompt.clone(),
                }),
                Some(old) => {
                    if let Some(agent) = self
                        .config
                        .user_agents
                        .iter_mut()
                        .find(|agent| agent.name == *old)
                    {
                        agent.name = name.clone();
                    }
                }
            }
            self.choose_agent(&name);
            self.go(Screen::Agents);
        }
        cancel
    }

    /// An agent's prompt: what is typed goes at its end, Backspace takes
    /// from it, Clear starts it over.
    fn prompt_page(&mut self, frame: &WidgetFrame, name: &str) -> bool {
        let page = &frame.page;
        let (mut save, mut clear, mut cancel) = (false, false, false);
        let mut flow = self.other.begin(page, frame.rect, frame.focused);
        flow.head("setting-typed", &tr("Edit prompt"));
        flow.note(name);
        let field = flow.field("", "", &mut self.editing, false);
        if flow.entry("chosen", &tr("Save"), None) {
            save = true;
        }
        if flow.entry("search-clear", &tr("Clear"), None) {
            clear = true;
        }
        if flow.entry("key-escape", &tr("Cancel"), None) {
            cancel = true;
        }
        flow.gap();
        for paragraph in self
            .editing
            .split("\n\n")
            .filter(|part| !part.trim().is_empty())
        {
            flow.note(paragraph.trim());
        }
        flow.finish();
        if clear {
            self.editing.clear();
        }
        if save || field.submitted {
            let prompt = self.editing.clone();
            if let Some(agent) = self
                .config
                .user_agents
                .iter_mut()
                .find(|agent| agent.name == name)
            {
                agent.prompt = prompt;
            }
            self.choose_agent(name);
            self.go(Screen::Agents);
        }
        cancel
    }

    /// The GTK settings' Speech page.
    fn speech_page(&mut self, frame: &WidgetFrame) -> bool {
        let page = &frame.page;
        enum Do {
            Backend,
            Whisper,
            Vosk,
            InstallVosk,
            HeyAlexy,
            Correction,
            Back,
        }
        let mut done = None;
        let languages = settings::vosk_languages();
        let mut flow = self.other.begin(page, frame.rect, frame.focused);
        flow.head("setting-microphone", &tr("Speech"));
        if let Some(status) = &self.status {
            flow.note(status);
        }
        flow.heading(&tr("Voice-to-Text Engine"));
        let whisper = self.config.stt_backend == "whisper";
        let engine = if whisper {
            tr("OpenAI Whisper (Recommended)")
        } else {
            tr("Vosk (Lightweight)")
        };
        if flow.entry("setting-microphone", &tr("STT Backend"), Some(&engine)) {
            done = Some(Do::Backend);
        }
        flow.gap();
        if whisper {
            flow.heading(&tr("Whisper Settings"));
            flow.note(&tr(
                "OpenAI Whisper provides high-accuracy offline transcription. Model is auto-downloaded on first use.",
            ));
            let size = WHISPER_MODELS
                .iter()
                .position(|model| *model == self.config.whisper_model)
                .map(|index| tr(WHISPER_LABELS[index]))
                .unwrap_or_else(|| self.config.whisper_model.clone());
            if flow.entry("file-drive", &tr("Model Size"), Some(&size)) {
                done = Some(Do::Whisper);
            }
        } else {
            flow.heading(&tr("Vosk Settings"));
            flow.note(&tr(
                "Vosk provides lightweight, streaming offline transcription.",
            ));
            let language = languages
                .iter()
                .find(|(code, _)| *code == self.config.vosk_lang)
                .map(|(_, name)| name.clone())
                .unwrap_or_else(|| self.config.vosk_lang.clone());
            if flow.entry("setting-typed", &tr("Vosk Language Model"), Some(&language)) {
                done = Some(Do::Vosk);
            }
            if !self.vosk_available {
                flow.note(&tr("Vosk is not installed"));
                if flow.detailed(
                    "add",
                    &tr("Install python-vosk"),
                    &tr("Install python-vosk to use the Vosk backend."),
                    None,
                ) {
                    done = Some(Do::InstallVosk);
                }
            }
        }
        flow.gap();
        flow.heading(&tr("Hey Alexy Wake Word"));
        if flow.switch(
            "",
            &tr("Enable \"Hey Alexy\""),
            self.config.hey_linux_enabled,
        ) {
            done = Some(Do::HeyAlexy);
        }
        flow.note(&tr(
            "Continuously listens for 'Hey Alexy' to activate the assistant. Uses openWakeWord for lightweight wake word detection.",
        ));
        flow.gap();
        flow.heading(&tr("Voice Correction"));
        if flow.switch(
            "",
            &tr("Enable for Direct API"),
            self.config.voice_correction_direct,
        ) {
            done = Some(Do::Correction);
        }
        flow.note(&tr(
            "Use an LLM to automatically fix transcribing errors. Note: This currently only functions when an online model (Direct API) is actively configured.",
        ));
        flow.gap();
        if flow.entry("arrow-left", &tr("Back"), None) {
            done = Some(Do::Back);
        }
        flow.finish();
        match done {
            Some(Do::Backend) => {
                let vosk = if self.vosk_available {
                    tr("Vosk (Lightweight)")
                } else {
                    tr("Vosk (Not Installed)")
                };
                let labels = [tr("OpenAI Whisper (Recommended)"), vosk];
                let commands: Vec<&str> = labels.iter().map(String::as_str).collect();
                page.menu_marked(
                    Some(&tr("STT Backend")),
                    &commands,
                    Some(usize::from(!whisper)),
                );
                self.menu = Some(Menu::SpeechBackend);
            }
            Some(Do::Whisper) => {
                let labels: Vec<String> = WHISPER_LABELS.iter().map(|label| tr(label)).collect();
                let commands: Vec<&str> = labels.iter().map(String::as_str).collect();
                let marked = WHISPER_MODELS
                    .iter()
                    .position(|model| *model == self.config.whisper_model);
                page.menu_marked(Some(&tr("Model Size")), &commands, marked);
                self.menu = Some(Menu::WhisperSize);
            }
            Some(Do::Vosk) => {
                let commands: Vec<&str> = languages.iter().map(|(_, name)| name.as_str()).collect();
                let marked = languages
                    .iter()
                    .position(|(code, _)| *code == self.config.vosk_lang);
                page.menu_marked(Some(&tr("Vosk Language Model")), &commands, marked);
                self.menu = Some(Menu::VoskLanguage(
                    languages.iter().map(|(code, _)| *code).collect(),
                ));
            }
            Some(Do::InstallVosk) => {
                self.status = Some(tr("Installing python-vosk"));
                self.run_job(JobKind::InstallVosk, INSTALL_VOSK, true);
            }
            Some(Do::HeyAlexy) => {
                let enabled = !self.config.hey_linux_enabled;
                self.config.hey_linux_enabled = enabled;
                self.config.save();
                voice::set_hey_linux(enabled);
            }
            Some(Do::Correction) => {
                self.config.voice_correction_direct = !self.config.voice_correction_direct;
                self.config.save();
            }
            Some(Do::Back) => return true,
            None => {}
        }
        false
    }

    /// Ollama's models listed again, on a thread.
    fn list_models(&mut self) {
        if self.models_loading.is_some() {
            return;
        }
        let (sender, loaded) = mpsc::channel();
        let worker = std::thread::Builder::new()
            .name("alexy-models".into())
            .spawn(move || {
                let _ = sender.send(settings::ollama_models());
            });
        if worker.is_ok() {
            self.models_loading = Some(loaded);
        }
    }

    // -- what arrives between frames ------------------------------------------

    fn collect(&mut self, page: &Page) {
        let mut events = Vec::new();
        if let Some(receiver) = &mut self.events {
            loop {
                match receiver.try_recv() {
                    Ok(event) => events.push(event),
                    Err(error) => {
                        if error.is_closed() {
                            self.events = None;
                        }
                        break;
                    }
                }
            }
        }
        for event in events {
            self.on_event(page, event);
        }

        if self.picking
            && let Some(path) = page.picked()
        {
            self.picking = false;
            self.attach(&path);
        }

        if let Some(loading) = &self.models_loading {
            match loading.try_recv() {
                Ok(models) => {
                    if self.config.local_model.is_empty()
                        && let Some((first, _)) = models.first()
                    {
                        self.config.local_model = first.clone();
                        self.config.save();
                    }
                    self.models = Some(models);
                    self.models_loading = None;
                }
                Err(TryRecvError::Disconnected) => self.models_loading = None,
                Err(TryRecvError::Empty) => {}
            }
        }

        if let Some(correcting) = &self.correcting {
            match correcting.try_recv() {
                Ok(corrected) => {
                    self.correcting = None;
                    self.proceed(&corrected, true);
                }
                Err(TryRecvError::Disconnected) => self.correcting = None,
                Err(TryRecvError::Empty) => {}
            }
        }

        if let Some(loading) = &self.loading {
            match loading.try_recv() {
                Ok(Ok(())) => {
                    self.loading = None;
                    self.record();
                }
                Ok(Err(error)) => {
                    self.loading = None;
                    self.status = None;
                    self.say(format!("{}{error}", tr("Error loading Whisper model: ")));
                }
                Err(TryRecvError::Disconnected) => self.loading = None,
                Err(TryRecvError::Empty) => {}
            }
        }

        let mut heard = Vec::new();
        if let Some(receiver) = &self.heard {
            loop {
                match receiver.try_recv() {
                    Ok(event) => heard.push(event),
                    Err(TryRecvError::Disconnected) => {
                        self.heard = None;
                        break;
                    }
                    Err(TryRecvError::Empty) => break,
                }
            }
        }
        for event in heard {
            match event {
                Heard::SpeakNow => self.status = Some(tr("Listening… (speak now)")),
                Heard::Transcribing => self.status = Some(tr("Transcribing…")),
                Heard::Text(text) => {
                    self.heard = None;
                    self.status = None;
                    self.send(&text, true);
                }
                Heard::Nothing | Heard::Stopped => {
                    self.heard = None;
                    self.status = None;
                }
            }
        }

        if let Some(starting) = &self.vosk_starting {
            match starting.try_recv() {
                Ok(VoskStart::Ready(session)) => {
                    self.vosk_starting = None;
                    self.record_vosk(session);
                }
                Ok(VoskStart::Missing) => {
                    self.vosk_starting = None;
                    self.status = None;
                    self.say(tr(
                        "python-vosk is not installed. You can install it from Settings.",
                    ));
                }
                Ok(VoskStart::Failed(error)) => {
                    self.vosk_starting = None;
                    self.status = None;
                    self.say(tr("Error loading voice model: {}").replacen("{}", &error, 1));
                }
                Err(TryRecvError::Disconnected) => self.vosk_starting = None,
                Err(TryRecvError::Empty) => {}
            }
        }
        let mut vosk = Vec::new();
        if let Some(receiver) = &self.vosk_heard {
            loop {
                match receiver.try_recv() {
                    Ok(event) => vosk.push(event),
                    Err(TryRecvError::Disconnected) => {
                        self.vosk_heard = None;
                        self.status = None;
                        break;
                    }
                    Err(TryRecvError::Empty) => break,
                }
            }
        }
        for event in vosk {
            match event {
                VoskHeard::Words(words) => self.draft = words,
                VoskHeard::Spoken => {
                    self.vosk_heard = None;
                    self.status = None;
                    let text = std::mem::take(&mut self.draft);
                    self.send(&text, true);
                }
                VoskHeard::Final(words) => self.draft = words,
            }
        }

        let mut lines = Vec::new();
        let mut done = None;
        if let Some(job) = &self.job {
            loop {
                match job.events.try_recv() {
                    Ok(JobEvent::Line(line)) => lines.push(line),
                    Ok(JobEvent::Done(success)) => {
                        done = Some(success);
                        break;
                    }
                    Err(TryRecvError::Disconnected) => {
                        done = Some(false);
                        break;
                    }
                    Err(TryRecvError::Empty) => break,
                }
            }
        }
        for line in lines {
            self.job_line(&line);
        }
        if let Some(success) = done
            && let Some(job) = self.job.take()
        {
            self.job_done(page, job.kind, success);
        }
    }

    fn on_event(&mut self, page: &Page, event: ToUi) {
        match event {
            ToUi::StreamBegin => {
                self.stream = Some((String::new(), String::new()));
                self.status = None;
            }
            ToUi::StreamPush(content, reasoning) => {
                if let Some((all_content, all_reasoning)) = &mut self.stream {
                    all_content.push_str(&content);
                    all_reasoning.push_str(&reasoning);
                }
            }
            ToUi::StreamFinish {
                reply,
                speak,
                final_,
            } => {
                let reasoning = self
                    .stream
                    .take()
                    .map(|(content, reasoning)| {
                        let inline = thought(&content);
                        if reasoning.trim().is_empty() {
                            inline
                        } else {
                            reasoning
                        }
                    })
                    .unwrap_or_default();
                if !reply.trim().is_empty() {
                    let mut said = Said::reply(plain(&reply));
                    said.reasoning = reasoning;
                    self.said.push(said);
                }
                if final_ {
                    self.busy = false;
                    self.status = None;
                    crate::screen::cleanup_tmp();
                    self.save_conversation();
                    if speak {
                        self.speak(&reply);
                    }
                } else {
                    self.status = Some(tr("Thinking..."));
                }
            }
            ToUi::StreamDiscard => self.stream = None,
            ToUi::ApiError(message) => self.api_error(&message),
            ToUi::Bubble(text) => self.say(text),
            ToUi::PullModel(model) => {
                self.status = Some(tr("Initiating download..."));
                self.run_job(
                    JobKind::Pull {
                        model: model.clone(),
                        resume: true,
                    },
                    &format!("ollama pull {model}"),
                    false,
                );
            }
            ToUi::InstallOllama => {
                page.ask(
                    &tr("Ollama is not installed"),
                    &tr("Install the Ollama daemon to use Local AI."),
                    &[&tr("Install Ollama"), &tr("Cancel")],
                );
                self.asking = Some(Question::InstallOllama);
            }
            ToUi::EndpointModel(model) => self.config.endpoint_model = model,
            ToUi::AskSafety { code, reply } => {
                let body = tr(
                    "The assistant wants to run the following command:\n\n{0}\n\nDo you want to allow this?",
                )
                .replacen("{0}", &code, 1);
                page.ask(
                    &tr("Command Execution Request"),
                    &body,
                    &[&tr("Allow"), &tr("Deny")],
                );
                self.asking = Some(Question::Safety(reply));
            }
            // pkexec asks for itself: no password is ever asked here.
            ToUi::AskPassword { reply, .. } => {
                let _ = reply.send(false);
            }
        }
    }

    fn take_answers(&mut self, page: &Page) {
        if self.asking.is_some()
            && let Some(answer) = page.answered()
        {
            match self.asking.take() {
                Some(Question::Safety(reply)) => {
                    let _ = reply.send(answer == 0);
                }
                Some(Question::InstallOllama) if answer == 0 => {
                    self.status = Some(tr("Downloading and installing Ollama daemon..."));
                    self.run_job(JobKind::InstallOllama, INSTALL_OLLAMA, true);
                }
                Some(Question::InstallOllama) => {
                    self.api_error(&tr("Ollama installation was cancelled or failed."))
                }
                Some(Question::Note) | None => {}
            }
        }
        if self.menu.is_some()
            && let Some(chosen) = page.chose()
        {
            match self.menu.take() {
                Some(Menu::Agents(names)) => {
                    if let Some(name) = names.get(chosen) {
                        let name = name.clone();
                        self.choose_agent(&name);
                    }
                }
                Some(Menu::Conversation(listed)) => match chosen {
                    0 => self.go(Screen::RenameConversation(listed.id)),
                    1 => self.delete_conversation(&listed.id),
                    _ => {}
                },
                Some(Menu::ConversationFilter(backends)) => {
                    if let Some(backend) = backends.get(chosen) {
                        self.conv_filter = Some(backend.clone());
                        self.other.reset();
                    }
                }
                Some(Menu::Backend) => {
                    if let Some((backend, _)) = BACKENDS.get(chosen) {
                        self.switch_backend(backend);
                    }
                }
                Some(Menu::Models(names)) => {
                    if let Some(name) = names.get(chosen) {
                        self.config.local_model = name.clone();
                        self.config.save();
                    }
                }
                Some(Menu::Popular) => {
                    if let Some(name) = REAL_POPULAR_NAMES.get(chosen) {
                        self.pull_name = (*name).to_owned();
                    }
                }
                Some(Menu::SpeechBackend) => {
                    let backend = if chosen == 0 { "whisper" } else { "vosk" };
                    if chosen <= 1 && self.config.stt_backend != backend {
                        self.voice.stop();
                        self.config.stt_backend = backend.into();
                        self.config.save();
                    }
                }
                Some(Menu::WhisperSize) => {
                    if let Some(model) = WHISPER_MODELS.get(chosen) {
                        self.config.whisper_model = (*model).into();
                        self.config.save();
                    }
                }
                Some(Menu::VoskLanguage(codes)) => {
                    if let Some(code) = codes.get(chosen) {
                        self.config.vosk_lang = (*code).into();
                        self.config.save();
                    }
                }
                None => {}
            }
        }
    }

    // -- the conversation ---------------------------------------------------------

    fn say(&mut self, text: String) {
        self.said.push(Said::reply(text));
    }

    fn reset_history(&mut self) {
        if let Ok(mut history) = self.shared.history.lock() {
            *history = vec![json!({"role": "system", "content": self.config.system_prompt})];
        }
        self.conv_agent = self.config.active_agent.clone();
    }

    /// The original `_save_conversation`.
    fn save_conversation(&mut self) {
        let history = self
            .shared
            .history
            .lock()
            .map(|history| history.clone())
            .unwrap_or_default();
        if history.len() <= 1 {
            return;
        }
        let created = self
            .conv_created
            .get_or_insert_with(conversations::now_iso)
            .clone();
        conversations::save(
            &self.conversation_id,
            &history,
            &created,
            &self.config.backend,
            &self.conv_agent,
        );
        self.saved = None;
    }

    fn new_conversation(&mut self) {
        self.save_conversation();
        self.conversation_id = glib::uuid_string_random().to_string();
        self.conv_created = None;
        self.reset_history();
        self.said = vec![greeting()];
        self.status = None;
    }

    /// The GTK panel's delete: the open conversation deleted starts anew.
    fn delete_conversation(&mut self, id: &str) {
        conversations::delete(id);
        self.saved = None;
        if id == self.conversation_id {
            self.conversation_id = glib::uuid_string_random().to_string();
            self.conv_created = None;
            self.reset_history();
            self.said = vec![greeting()];
            self.status = None;
        }
        self.other.reset();
    }

    /// The original `_load_conversation`, shown as this page shows messages.
    fn load_conversation(&mut self, id: &str) {
        let Some(saved) = conversations::load(id) else {
            return;
        };
        self.save_conversation();
        self.conversation_id = saved.id;
        if let Ok(mut history) = self.shared.history.lock() {
            *history = saved.history.clone();
        }
        self.conv_created = Some(saved.created);
        self.conv_agent = saved.agent;
        let mut backend = saved.backend.unwrap_or_else(|| self.config.backend.clone());
        if backend == "qwen_cli" {
            backend = "direct".into();
        }
        self.config.backend = backend;
        static COMMAND_ONLY: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"(?s)^```(?:bash|sh)\n.*?```$").expect("valid"));
        self.said.clear();
        for message in &saved.history {
            let content = message.get("content").cloned().unwrap_or(Value::Null);
            let content = conversations::strip_system_instructions(&content);
            let text = conversations::extract_text(&content);
            let images = match &content {
                Value::Array(parts) => parts
                    .iter()
                    .filter(|part| part.get("type").and_then(Value::as_str) == Some("image_url"))
                    .count(),
                _ => 0,
            };
            match message.get("role").and_then(Value::as_str) {
                Some("user") if !text.starts_with("System Command Execution Results:") => {
                    self.said.push(Said {
                        user: true,
                        text,
                        reasoning: String::new(),
                        open: false,
                        images,
                    });
                }
                Some("assistant") if !COMMAND_ONLY.is_match(conversations::python_strip(&text)) => {
                    let mut said = Said::reply(plain(&visible(&text)));
                    said.reasoning = thought(&text);
                    self.said.push(said);
                }
                _ => {}
            }
        }
        self.status = None;
    }

    // -- a turn --------------------------------------------------------------------

    /// An image file attached for the next message.
    fn attach(&mut self, path: &Path) {
        match std::fs::read(path) {
            Ok(raw) => {
                if !looks_like_image(&raw) {
                    println!(
                        "[Image] Failed to load file {}: not an image",
                        path.display()
                    );
                    return;
                }
                let name = path
                    .file_name()
                    .map(|name| name.to_string_lossy().into_owned())
                    .unwrap_or_default();
                self.attached.push(Attached {
                    mime: crate::ui::guess_mime_type(path).into(),
                    data: base64::engine::general_purpose::STANDARD.encode(&raw),
                    name,
                });
            }
            Err(error) => println!("[Image] Failed to load file {}: {error}", path.display()),
        }
    }

    /// The original `on_send_clicked`, for what is typed or heard.
    fn send(&mut self, text: &str, voice: bool) {
        let text = conversations::python_strip(text).to_owned();
        if (text.is_empty() && self.attached.is_empty()) || self.busy || self.correcting.is_some() {
            return;
        }
        if self.config.backend == "direct" && self.config.api_key.is_empty() {
            self.say(tr("Please configure your API Key in settings first."));
            return;
        }
        self.shared.speak_next.store(voice, Ordering::SeqCst);
        self.shared.abort.store(false, Ordering::SeqCst);
        if voice && self.config.backend == "direct" && self.config.voice_correction_direct {
            let (sender, corrected) = mpsc::channel();
            let config = self.config.clone();
            std::thread::spawn(move || {
                let _ = sender.send(crate::ai::correct_voice_text(
                    &text,
                    &config.api_url,
                    &config.api_key,
                    &config.model,
                ));
            });
            self.status = Some(tr("Thinking..."));
            self.correcting = Some(corrected);
            return;
        }
        self.proceed(&text, voice);
    }

    /// The original `_proceed_with_message`: the message (with the images
    /// attached, and the screen where Screen Awareness is on) sent.
    fn proceed(&mut self, text: &str, _voice: bool) {
        let images: Vec<(String, String)> = std::mem::take(&mut self.attached)
            .into_iter()
            .map(|attached| (attached.mime, attached.data))
            .collect();
        let shown = images.len();
        self.said.push(Said {
            user: true,
            text: text.to_owned(),
            reasoning: String::new(),
            open: false,
            images: shown,
        });
        self.busy = true;
        self.status = Some(tr("Thinking..."));
        let screen = self.screen_awareness;
        let shared = self.shared.clone();
        let text = text.to_owned();
        let failed = tr(
            "Failed to capture screenshot. No screenshot tool found (grim, gnome-screenshot, spectacle, scrot, or import).",
        );
        self.start_turn(move |turn| {
            let mut images = images;
            let mut captured = false;
            if screen {
                match crate::screen::capture_screenshot() {
                    Some(image) => {
                        images.push(image);
                        captured = true;
                    }
                    None => {
                        let _ = turn.ui.unbounded_send(ToUi::Bubble(failed));
                    }
                }
            }
            let content = if images.is_empty() {
                json!(text)
            } else {
                let mut parts = Vec::new();
                if !text.is_empty() {
                    let sent = if captured {
                        format!("{}{text}", conversations::SCREEN_AWARENESS_PREFIX)
                    } else {
                        text.clone()
                    };
                    parts.push(json!({"type": "text", "text": sent}));
                }
                for (mime, data) in images {
                    parts.push(json!({
                        "type": "image_url",
                        "image_url": {"url": format!("data:{mime};base64,{data}")},
                    }));
                }
                Value::Array(parts)
            };
            if let Ok(mut history) = shared.history.lock() {
                history.push(json!({"role": "user", "content": content}));
            }
        });
    }

    /// Run a turn on a thread of its own; `prepare` runs there first.
    fn start_turn(&mut self, prepare: impl FnOnce(&Turn) + Send + 'static) {
        let config = &self.config;
        let settings = Settings {
            backend: config.backend.clone(),
            api_url: config.api_url.clone(),
            api_key: config.api_key.clone(),
            model: config.model.clone(),
            local_model: config.local_model.clone(),
            local_url: config.local_url.clone(),
            endpoint_url: config.endpoint_url.clone(),
            endpoint_model: config.endpoint_model.clone(),
            auto_execute_commands: config.auto_execute_commands,
        };
        let (sender, events) = futures_channel::mpsc::unbounded::<ToUi>();
        let mut turn = Turn {
            settings,
            shared: self.shared.clone(),
            host: api::host(),
            ui: sender,
            pkexec: true,
        };
        self.events = Some(events);
        let spawned = std::thread::Builder::new()
            .name("alexy-ai".into())
            .spawn(move || {
                let run = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    prepare(&turn);
                    turn.call_ai();
                }));
                if run.is_err() {
                    let _ = turn
                        .ui
                        .unbounded_send(ToUi::ApiError("Error: the request failed".into()));
                }
            });
        if let Err(error) = spawned {
            self.api_error(&format!("Error: {error}"));
        }
    }

    /// The original `on_api_error`.
    fn api_error(&mut self, message: &str) {
        self.stream = None;
        self.say(format!("{}{message}", tr("⚠️ Error: ")));
        if let Ok(mut history) = self.shared.history.lock()
            && history.len() > 1
        {
            history.pop();
        }
        self.busy = false;
        self.status = None;
        crate::screen::cleanup_tmp();
    }

    /// The original `cancel_generation` (and stopping a recording).
    fn stop(&mut self) {
        if self.listening() {
            self.voice.stop();
            self.loading = None;
            self.vosk_starting = None;
            self.vosk_heard = None;
            self.status = None;
            return;
        }
        if self.correcting.take().is_some() {
            self.status = None;
            return;
        }
        if !self.busy {
            self.stop_speaking();
            return;
        }
        self.stop_speaking();
        self.shared.abort.store(true, Ordering::SeqCst);
        self.busy = false;
        self.stream = None;
        self.events = None;
        self.job = None;
        self.status = None;
        self.say(tr("Generation stopped by user."));
    }

    // -- voice ----------------------------------------------------------------------

    /// The original `on_stt_toggled`: Whisper or Vosk, as the settings say.
    fn listen(&mut self) {
        if self.listening() || self.busy || self.job.is_some() {
            return;
        }
        self.stop_speaking();
        if self.config.stt_backend == "vosk" {
            self.listen_vosk();
            return;
        }
        if !self.whisper_available {
            self.status = Some(tr(
                "openai-whisper is not installed. Install it via: pip install openai-whisper",
            ));
            return;
        }
        let model = self.config.whisper_model.clone();
        if self.voice.model_loaded(&model) {
            self.record();
            return;
        }
        if !voice::whisper_model_file(&model).exists() {
            match voice::whisper_download(&model) {
                Some((size, command)) => {
                    self.status = Some(
                        tr("Downloading Whisper {} model ({})...")
                            .replacen("{}", &model, 1)
                            .replacen("{}", size, 1),
                    );
                    self.run_job(JobKind::Whisper, &command, false);
                }
                None => self.say(tr("Unknown Whisper model: {}").replacen("{}", &model, 1)),
            }
            return;
        }
        self.status = Some(tr("Loading voice model…"));
        let (sender, loading) = mpsc::channel();
        let voice = self.voice.clone();
        std::thread::spawn(move || {
            let _ = sender.send(voice.load_whisper(&model));
        });
        self.loading = Some(loading);
    }

    /// The original `_stt_start_vosk`, on this page.
    fn listen_vosk(&mut self) {
        if !self.vosk_available {
            self.status = Some(tr(
                "python-vosk is not installed. You can install it from Settings.",
            ));
            return;
        }
        let lang = self.config.vosk_lang.clone();
        let model = voice::vosk_model_dir(&lang);
        if !model.exists() {
            self.status = Some(tr("Downloading Offline Voice Model"));
            self.run_job(JobKind::VoskModel, &voice::vosk_download(&lang), false);
            return;
        }
        self.status = Some(tr("Loading voice model…"));
        let (sender, starting) = mpsc::channel();
        let model = model.to_string_lossy().into_owned();
        std::thread::spawn(move || {
            let _ = sender.send(voice::start_vosk(&model));
        });
        self.vosk_starting = Some(starting);
    }

    fn record_vosk(&mut self, session: voice::VoskSession) {
        voice::play_activation_sound();
        let audio = match self.voice.start_arecord() {
            Ok(audio) => audio,
            Err(error) => {
                self.status = None;
                self.say(tr("Failed to start mic: {}").replacen("{}", &error.to_string(), 1));
                return;
            }
        };
        self.status = Some(tr("Listening..."));
        let (sender, heard) = mpsc::channel();
        let voice = self.voice.clone();
        std::thread::spawn(move || {
            voice.record_vosk(session, audio, |event| {
                let _ = sender.send(event);
            });
        });
        self.vosk_heard = Some(heard);
    }

    /// The original `_begin_whisper_recording`, on this page.
    fn record(&mut self) {
        voice::play_activation_sound();
        let audio = match self.voice.start_arecord() {
            Ok(audio) => audio,
            Err(error) => {
                self.status = None;
                self.say(tr("Failed to start mic: {}").replacen("{}", &error.to_string(), 1));
                return;
            }
        };
        self.status = Some(tr("Listening…"));
        let (sender, heard) = mpsc::channel();
        let voice = self.voice.clone();
        std::thread::spawn(move || {
            voice.record(audio, |event| {
                let _ = sender.send(event);
            });
        });
        self.heard = Some(heard);
    }

    /// Read a reply aloud (the original `play_tts`), fetching Piper first
    /// where it is missing.
    fn speak(&mut self, reply: &str) {
        let speech = tts::plan(
            reply,
            &self.config.stt_backend,
            &self.config.vosk_lang,
            &self.voice.detected_lang(),
        );
        match speech {
            None => {}
            Some(Speech::Espeak { voice, text }) => {
                println!("Executing fallback TTS (espeak-ng): -v {voice}");
                let mut command = Command::new("espeak-ng");
                command.args(["-v", &voice, &text]);
                self.start_speaking(command);
            }
            Some(Speech::Piper {
                line,
                download: Some(download),
            }) => {
                self.status = Some(tr("Downloading Neural TTS Engine/Voice"));
                self.run_job(JobKind::Speech(line), &download, false);
            }
            Some(Speech::Piper {
                line,
                download: None,
            }) => self.run_piper(&line),
        }
    }

    fn run_piper(&mut self, line: &str) {
        println!("Executing TTS: {line}");
        let mut command = Command::new("bash");
        command.args(["-c", line]);
        self.start_speaking(command);
    }

    fn start_speaking(&mut self, command: Command) {
        self.stop_speaking();
        match tts::speaking(command) {
            Ok(child) => self.tts = Some(child),
            Err(error) => println!("TTS failed: {error}"),
        }
    }

    fn stop_speaking(&mut self) {
        if let Some(child) = self.tts.take() {
            tts::silence(child);
        }
    }

    // -- jobs -----------------------------------------------------------------------

    /// Run `command` under bash (through pkexec when `privileged`), its
    /// output read a line at a time (`\r` ends one too).
    fn run_job(&mut self, kind: JobKind, command: &str, privileged: bool) {
        if self.job.is_some() {
            return;
        }
        let mut argv = vec!["bash".to_owned(), "-c".to_owned(), command.to_owned()];
        if privileged {
            argv.insert(0, "pkexec".into());
        }
        let (sender, events) = mpsc::channel();
        let spawned = (|| -> std::io::Result<(Child, std::io::PipeReader)> {
            let (reader, writer) = std::io::pipe()?;
            let errors = writer.try_clone()?;
            let child = Command::new(&argv[0])
                .args(&argv[1..])
                .stdin(Stdio::null())
                .stdout(writer)
                .stderr(errors)
                .spawn()?;
            Ok((child, reader))
        })();
        let (mut child, mut reader) = match spawned {
            Ok(spawned) => spawned,
            Err(_) => {
                self.finish_failed_start(kind);
                return;
            }
        };
        std::thread::spawn(move || {
            let mut line = Vec::new();
            let mut byte = [0u8; 1];
            let send = |line: &mut Vec<u8>| {
                if !line.is_empty() {
                    let _ = sender.send(JobEvent::Line(String::from_utf8_lossy(line).into_owned()));
                    line.clear();
                }
            };
            // The pipe ends when the command and everything it started
            // have closed it.
            while let Ok(1) = reader.read(&mut byte) {
                if byte[0] == b'\n' || byte[0] == b'\r' {
                    send(&mut line);
                } else {
                    line.push(byte[0]);
                }
            }
            send(&mut line);
            let success = child.wait().is_ok_and(|status| status.success());
            let _ = sender.send(JobEvent::Done(success));
        });
        self.job = Some(Job { kind, events });
    }

    /// A job that could not even start ends as one that failed.
    fn finish_failed_start(&mut self, kind: JobKind) {
        let (sender, events) = mpsc::channel();
        let _ = sender.send(JobEvent::Done(false));
        self.job = Some(Job { kind, events });
    }

    /// A line of a job's output: what the GTK progress window would say.
    fn job_line(&mut self, line: &str) {
        static PERCENT: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"(\d+(?:\.\d+)?)%").expect("valid"));
        let Some(job) = &self.job else {
            return;
        };
        let lower = line.to_lowercase();
        let status = match &job.kind {
            JobKind::Pull { model, .. } => {
                if lower.contains("pulling manifest") {
                    tr("Fetching model info for {}...").replacen("{}", model, 1)
                } else if lower.contains("pulling") {
                    let mut status = tr("Downloading {}...").replacen("{}", model, 1);
                    if let Some(percent) = PERCENT.captures(line) {
                        status.push_str(&format!(" {}%", &percent[1]));
                    }
                    status
                } else if lower.contains("verifying") {
                    tr("Verifying download integrity...")
                } else if lower.contains("writing") {
                    tr("Finalizing {}...").replacen("{}", model, 1)
                } else if lower.contains("success") {
                    tr("Successfully downloaded {}!").replacen("{}", model, 1)
                } else {
                    return;
                }
            }
            JobKind::Whisper => match PERCENT.captures(line) {
                Some(percent) => format!(
                    "{}  {}%",
                    tr("Downloading Voice Recognition Model"),
                    &percent[1]
                ),
                None => return,
            },
            _ => {
                let shown: String = line.trim().chars().take(60).collect();
                if shown.is_empty() {
                    return;
                }
                shown
            }
        };
        self.status = Some(status);
    }

    fn job_done(&mut self, page: &Page, kind: JobKind, success: bool) {
        self.status = None;
        match kind {
            JobKind::InstallOllama if success => {
                let _ = Command::new("systemctl")
                    .args(["enable", "--now", "ollama"])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status();
                self.list_models();
                if self.busy {
                    if self.config.local_model.is_empty() {
                        self.api_error(&tr(
                            "Ollama Installed! Please open Settings to pull an AI model.",
                        ));
                    } else {
                        self.status = Some(tr("Thinking..."));
                        self.start_turn(|_| {});
                    }
                }
            }
            JobKind::InstallOllama => {
                if self.busy {
                    self.api_error(&tr("Ollama installation was cancelled or failed."));
                } else {
                    self.status = Some(tr("Ollama installation was cancelled or failed."));
                }
            }
            JobKind::UninstallOllama => {
                self.models = None;
                if success {
                    self.status = Some(tr("Ollama has been uninstalled"));
                }
            }
            JobKind::Pull { resume: true, .. } if success => {
                self.status = Some(tr("Thinking..."));
                self.start_turn(|_| {});
            }
            JobKind::Pull { resume: true, .. } => {
                self.api_error(&tr("Model download was cancelled or failed."))
            }
            JobKind::Pull {
                model,
                resume: false,
            } => {
                if success {
                    self.config.local_model = model.clone();
                    self.config.save();
                    self.status = Some(tr("Successfully downloaded {}!").replacen("{}", &model, 1));
                } else {
                    self.status = Some(tr("Model download was cancelled or failed."));
                }
                self.list_models();
            }
            JobKind::RemoveModel(model) => {
                if success {
                    page.ask(
                        &tr("Remove Model"),
                        &tr("Model {} has been successfully removed.").replacen("{}", &model, 1),
                        &[&tr("OK")],
                    );
                    self.asking = Some(Question::Note);
                    if self.config.local_model == model {
                        self.config.local_model = String::new();
                        self.config.save();
                    }
                }
                self.list_models();
            }
            JobKind::Whisper if success => self.listen(),
            JobKind::Whisper => {
                let _ = std::fs::remove_file(voice::whisper_model_file(&self.config.whisper_model));
                self.status = Some(tr("Failed to download Whisper model."));
            }
            JobKind::VoskModel if success => self.listen(),
            JobKind::VoskModel => self.status = Some(tr("Failed to download voice model.")),
            JobKind::InstallVosk => {
                self.vosk_available = voice::python_has("vosk");
                if success && self.vosk_available {
                    page.ask(
                        &tr("Vosk installed successfully!"),
                        &tr("Vosk backend is now available."),
                        &[&tr("OK")],
                    );
                    self.asking = Some(Question::Note);
                }
            }
            JobKind::Speech(line) if success => self.run_piper(&line),
            JobKind::Speech(_) => {}
        }
    }
}

/// A setting that is typed: its name, then a field with it (or, where the
/// toolkit cannot take text, what it is set to).
fn text_setting(flow: &mut FlowFrame, typing: bool, name: &str, value: &mut String, secret: bool) {
    if typing {
        flow.note(name);
        flow.field("", name, value, secret);
    } else {
        let shown = if secret && !value.is_empty() {
            "\u{2022}".repeat(8)
        } else {
            value.clone()
        };
        flow.entry("", name, Some(&shown));
    }
}

/// Whether `raw` starts as an image file does (PNG, JPEG, GIF, BMP, TIFF,
/// WebP): what the GTK page's texture loading would accept.
fn looks_like_image(raw: &[u8]) -> bool {
    raw.starts_with(b"\x89PNG\r\n\x1a\n")
        || raw.starts_with(&[0xff, 0xd8, 0xff])
        || raw.starts_with(b"GIF87a")
        || raw.starts_with(b"GIF89a")
        || raw.starts_with(b"BM")
        || raw.starts_with(b"II*\0")
        || raw.starts_with(b"MM\0*")
        || (raw.len() > 12 && raw.starts_with(b"RIFF") && &raw[8..12] == b"WEBP")
}

/// A backend's name in the settings.
fn backend_label(backend: &str) -> String {
    BACKENDS
        .iter()
        .find(|(key, _)| *key == backend)
        .map(|(_, label)| tr(label))
        .unwrap_or_else(|| backend.to_owned())
}

/// A backend's name in the conversations' filter.
fn backend_name(backend: &str) -> String {
    match backend {
        "direct" => tr("Online API"),
        "endpoint" => tr("Local AI (Endpoint)"),
        _ => tr("Local AI"),
    }
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(glib::home_dir)
}

/// The reply as it is shown so far: its inline `<think>` blocks are the
/// model's reasoning, not its answer.
fn visible(content: &str) -> String {
    static THINK: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?s)<think>.*?(</think>|$)").expect("valid"));
    THINK.replace_all(content, "").into_owned()
}

/// What the inline `<think>` blocks of a reply say.
fn thought(content: &str) -> String {
    static THINK: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?s)<think>(.*?)(?:</think>|$)").expect("valid"));
    THINK
        .captures_iter(content)
        .map(|captures| captures[1].trim().to_owned())
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("\n\n")
}

/// Markdown as plain text: the page's labels show words, not markup.
fn plain(text: &str) -> String {
    static FENCE: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"```[a-zA-Z0-9]*\n?").expect("valid"));
    static BOLD: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?s)\*\*(.*?)\*\*").expect("valid"));
    static HEADING: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?m)^#{1,6}\s+").expect("valid"));
    static BULLET: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?m)^[-*]\s+").expect("valid"));
    static ITALIC: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"\*([^*\s][^*\n]*?)\*").expect("valid"));
    let text = FENCE.replace_all(text, "");
    let text = BOLD.replace_all(&text, "$1");
    let text = HEADING.replace_all(&text, "");
    let text = BULLET.replace_all(&text, "• ");
    let text = ITALIC.replace_all(&text, "$1");
    conversations::python_strip(&text.replace('`', "")).to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replies_are_shown_as_plain_words() {
        assert_eq!(
            plain("## Title\nSome **bold** `code` and:\n- one\n* two\n```bash\nls -l\n```"),
            "Title\nSome bold code and:\n• one\n• two\nls -l"
        );
        assert_eq!(
            plain("an *italic* word, 2 * 3 * 4"),
            "an italic word, 2 * 3 * 4"
        );
        assert_eq!(visible("<think>hmm</think>Answer"), "Answer");
        assert_eq!(visible("<think>still thinking"), "");
        assert_eq!(
            thought("<think> hmm </think>Answer<think>more</think>"),
            "hmm\n\nmore"
        );
        assert_eq!(thought("Answer"), "");
    }

    #[test]
    fn back_goes_up_one_page() {
        assert_eq!(Screen::Chat.back(), None);
        assert_eq!(Screen::Speech.back(), Some(Screen::Settings));
        assert_eq!(Screen::Settings.back(), Some(Screen::Chat));
        assert_eq!(Screen::Prompt("x".into()).back(), Some(Screen::Agents));
        assert_eq!(
            Screen::RenameConversation("x".into()).back(),
            Some(Screen::Conversations)
        );
    }
}
