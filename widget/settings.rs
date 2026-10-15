//! The settings sidebar: the original's `on_settings_clicked`,
//! `_build_agents_page`, `_close_settings_sidebar` and the Ollama helpers
//! (`_refresh_ollama_models`, `get_ollama_models`, `on_pull_ollama_clicked`,
//! `on_remove_ollama_clicked`, `on_ollama_install_clicked`).

use crate::config::{self, Agent, BUILTIN_AGENT_NAMES, DEFAULT_AGENT_NAME};
use crate::progress::{self, OnClose};
use crate::theme;
use crate::ui::{Alexy, guard, set_button_icon, tr};
use crate::worker::ollama_installed;
use adw::prelude::*;
use gtk::glib;
use serde_json::json;
use std::cell::{Cell, RefCell};
use std::rc::Rc;

pub(crate) const POPULAR_NAMES: [&str; 9] = [
    "qwen3.5 (9b)",
    "qwen3 (8b)",
    "qwen2.5-coder (7b)",
    "llama3.2 (3b)",
    "llama3.1 (8b)",
    "mistral (7b)",
    "gemma2 (9b)",
    "phi3 (8b)",
    "deepseek-coder-v2 (16b)",
];
pub(crate) const REAL_POPULAR_NAMES: [&str; 9] = [
    "qwen3.5",
    "qwen3",
    "qwen2.5-coder",
    "llama3.2",
    "llama3.1",
    "mistral",
    "gemma2",
    "phi3",
    "deepseek-coder-v2",
];
pub(crate) const WHISPER_MODELS: [&str; 4] = ["tiny", "base", "small", "medium"];
/// The original's Ollama install and uninstall lines (run as root).
pub(crate) const INSTALL_OLLAMA: &str = "curl -fsSL https://ollama.com/install.sh | sh";
pub(crate) const UNINSTALL_OLLAMA: &str = "pacman -Qi ollama &>/dev/null && pacman -Rns ollama --noconfirm || { systemctl disable --now ollama 2>/dev/null; rm -f /usr/local/bin/ollama; rm -rf /usr/local/lib/ollama; rm -f /etc/systemd/system/ollama.service; systemctl daemon-reload; }";
/// The original's python-vosk install line (run as root).
pub(crate) const INSTALL_VOSK: &str = "pacman -Sy python-vosk --noconfirm";

/// `self.vosk_available_langs`.
pub(crate) fn vosk_languages() -> Vec<(&'static str, String)> {
    vec![
        ("small-en-us-0.15", tr("English (United States)")),
        ("small-en-in-0.4", tr("English (India)")),
        ("small-cn-0.22", tr("Chinese")),
        ("small-fr-0.22", tr("French")),
        ("small-de-0.15", tr("German")),
        ("small-es-0.42", tr("Spanish")),
        ("small-pt-0.3", tr("Portuguese")),
        ("small-it-0.22", tr("Italian")),
        ("small-ru-0.22", tr("Russian")),
        ("small-uk-v3-nano", tr("Ukrainian")),
        ("small-pl-0.22", tr("Polish")),
        ("small-ja-0.22", tr("Japanese")),
        ("small-ko-0.22", tr("Korean")),
    ]
}

fn string_list(items: &[String]) -> gtk::StringList {
    let list = gtk::StringList::new(&[]);
    for item in items {
        list.append(item);
    }
    list
}

/// The original `get_ollama_models`: `(name, size)` of `ollama list`.
pub fn ollama_models() -> Vec<(String, String)> {
    if !ollama_installed() {
        return Vec::new();
    }
    let Ok(output) = std::process::Command::new("ollama").arg("list").output() else {
        return Vec::new();
    };
    if !output.status.success() {
        return Vec::new();
    }
    let text = String::from_utf8_lossy(&output.stdout);
    crate::conversations::python_strip(&text)
        .split('\n')
        .skip(1)
        .filter_map(|line| {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() < 3 {
                return None;
            }
            let size = parts
                .iter()
                .position(|part| ["GB", "MB", "KB", "B"].contains(part))
                .filter(|index| *index > 0)
                .map(|index| format!("{} {}", parts[index - 1], parts[index]))
                .unwrap_or_else(|| "Unknown Size".into());
            Some((parts[0].to_owned(), size))
        })
        .collect()
}

/// The Agents page's working copy and editor.
struct Agents {
    alexy: std::rc::Weak<Alexy>,
    working: RefCell<Vec<Agent>>,
    combo: adw::ComboRow,
    editor_box: gtk::Box,
    current_name: RefCell<String>,
    buffer: RefCell<Option<gtk::TextBuffer>>,
    loading: Cell<bool>,
    default_prompt: String,
}

impl Agents {
    fn names(&self) -> Vec<String> {
        BUILTIN_AGENT_NAMES
            .iter()
            .map(|name| name.to_string())
            .chain(self.working.borrow().iter().map(|agent| agent.name.clone()))
            .collect()
    }

    fn clear_box(&self) {
        while let Some(child) = self.editor_box.first_child() {
            self.editor_box.remove(&child);
        }
    }

    fn commit_editor(&self) {
        let name = self.current_name.borrow().clone();
        if name.is_empty() || BUILTIN_AGENT_NAMES.contains(&name.as_str()) {
            return;
        }
        if let Some(buffer) = self.buffer.borrow().as_ref() {
            let text = buffer
                .text(&buffer.start_iter(), &buffer.end_iter(), false)
                .to_string();
            if let Some(agent) = self
                .working
                .borrow_mut()
                .iter_mut()
                .find(|agent| agent.name == name)
            {
                agent.prompt = text;
            }
        }
    }

    fn prompt_view(text: &str, editable: bool) -> (gtk::ScrolledWindow, gtk::TextView) {
        let view = gtk::TextView::new();
        view.set_wrap_mode(gtk::WrapMode::WordChar);
        view.set_editable(editable);
        view.set_cursor_visible(editable);
        view.set_top_margin(8);
        view.set_bottom_margin(8);
        view.set_left_margin(8);
        view.set_right_margin(8);
        view.buffer().set_text(text);
        if !editable {
            view.add_css_class("dim-label");
        }
        let scroll = gtk::ScrolledWindow::new();
        scroll.set_policy(gtk::PolicyType::Never, gtk::PolicyType::Automatic);
        scroll.set_min_content_height(220);
        scroll.set_child(Some(&view));
        scroll.add_css_class("card");
        (scroll, view)
    }

    fn build_editor(self: &Rc<Self>, name: &str) {
        self.clear_box();
        self.current_name.replace(name.to_owned());
        self.buffer.replace(None);
        if BUILTIN_AGENT_NAMES.contains(&name) {
            let info = gtk::Label::new(Some(&tr(
                "This is a built-in agent. Its prompt is shown for reference and cannot be edited.",
            )));
            info.add_css_class("dim-label");
            info.add_css_class("caption");
            info.set_wrap(true);
            info.set_halign(gtk::Align::Start);
            self.editor_box.append(&info);
            let prompt = config::agent_prompt(name, &self.working.borrow(), &self.default_prompt);
            let (scroll, _) = Self::prompt_view(&prompt, false);
            self.editor_box.append(&scroll);
            return;
        }
        let prompt = self
            .working
            .borrow()
            .iter()
            .find(|agent| agent.name == name)
            .map(|agent| agent.prompt.clone())
            .unwrap_or_else(|| self.default_prompt.clone());
        let (scroll, view) = Self::prompt_view(&prompt, true);
        self.buffer.replace(Some(view.buffer()));
        self.editor_box.append(&scroll);

        let buttons = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        buttons.set_halign(gtk::Align::End);
        let rename = gtk::Button::with_label(&tr("Rename Agent"));
        let weak = Rc::downgrade(self);
        let current = name.to_owned();
        rename.connect_clicked(move |_| {
            if let Some(agents) = weak.upgrade() {
                guard("rename agent", || agents.on_rename(&current));
            }
        });
        buttons.append(&rename);
        let delete = gtk::Button::with_label(&tr("Delete Agent"));
        delete.add_css_class("destructive-action");
        let weak = Rc::downgrade(self);
        let current = name.to_owned();
        delete.connect_clicked(move |_| {
            if let Some(agents) = weak.upgrade() {
                guard("delete agent", || {
                    agents
                        .working
                        .borrow_mut()
                        .retain(|agent| agent.name != current);
                    agents.buffer.replace(None);
                    agents.current_name.replace(DEFAULT_AGENT_NAME.into());
                    agents.refresh_combo(DEFAULT_AGENT_NAME);
                });
            }
        });
        buttons.append(&delete);
        self.editor_box.append(&buttons);
    }

    fn name_dialog(
        &self,
        heading: &str,
        body: &str,
        response: &str,
        label: &str,
    ) -> (adw::MessageDialog, gtk::Entry) {
        let parent = self
            .alexy
            .upgrade()
            .and_then(|alexy| alexy.window_or_root());
        let dialog = adw::MessageDialog::new(parent.as_ref(), Some(heading), Some(body));
        let entry = gtk::Entry::new();
        dialog.set_extra_child(Some(&entry));
        dialog.add_response("cancel", &tr("Cancel"));
        dialog.add_response(response, label);
        dialog.set_response_appearance(response, adw::ResponseAppearance::Suggested);
        dialog.set_default_response(Some(response));
        let weak_dialog = dialog.downgrade();
        let response = response.to_owned();
        entry.connect_activate(move |_| {
            if let Some(dialog) = weak_dialog.upgrade() {
                dialog.response(&response);
            }
        });
        (dialog, entry)
    }

    fn on_rename(self: &Rc<Self>, name: &str) {
        self.commit_editor();
        let (dialog, entry) = self.name_dialog(
            &tr("Rename Agent"),
            &tr("Enter a new name for this agent."),
            "rename",
            &tr("Rename"),
        );
        entry.set_text(name);
        let weak = Rc::downgrade(self);
        let name = name.to_owned();
        dialog.connect_response(None, move |dialog, response| {
            if response == "rename"
                && let Some(agents) = weak.upgrade()
            {
                let new_name = crate::conversations::python_strip(&entry.text()).to_owned();
                let existing: Vec<String> = agents
                    .names()
                    .iter()
                    .map(|n| n.to_lowercase())
                    .filter(|n| *n != name.to_lowercase())
                    .collect();
                if !new_name.is_empty() && !existing.contains(&new_name.to_lowercase()) {
                    if let Some(agent) = agents
                        .working
                        .borrow_mut()
                        .iter_mut()
                        .find(|agent| agent.name == name)
                    {
                        agent.name = new_name.clone();
                    }
                    agents.current_name.replace(new_name.clone());
                    guard("refresh", || agents.refresh_combo(&new_name));
                }
            }
            dialog.destroy();
        });
        dialog.present();
    }

    fn on_new_agent(self: &Rc<Self>) {
        self.commit_editor();
        let (dialog, entry) = self.name_dialog(
            &tr("New Agent"),
            &tr("Enter a name for the new agent."),
            "create",
            &tr("Create"),
        );
        entry.set_placeholder_text(Some(&tr("Agent name")));
        let weak = Rc::downgrade(self);
        dialog.connect_response(None, move |dialog, response| {
            if response == "create"
                && let Some(agents) = weak.upgrade()
            {
                let name = crate::conversations::python_strip(&entry.text()).to_owned();
                let existing: Vec<String> =
                    agents.names().iter().map(|n| n.to_lowercase()).collect();
                if !name.is_empty() && !existing.contains(&name.to_lowercase()) {
                    agents.working.borrow_mut().push(Agent {
                        name: name.clone(),
                        prompt: agents.default_prompt.clone(),
                    });
                    guard("refresh", || agents.refresh_combo(&name));
                }
            }
            dialog.destroy();
        });
        dialog.present();
    }

    fn refresh_combo(self: &Rc<Self>, select: &str) {
        self.loading.set(true);
        let names = self.names();
        self.combo.set_model(Some(&string_list(&names)));
        let index = names.iter().position(|name| name == select).unwrap_or(0);
        self.combo.set_selected(index as u32);
        self.loading.set(false);
        self.build_editor(&names[index]);
    }

    /// `get_state`: the edited agents and the chosen one.
    fn state(&self) -> (Vec<Agent>, String) {
        self.commit_editor();
        let names = self.names();
        let selected = self.combo.selected() as usize;
        let active = names
            .get(selected)
            .cloned()
            .unwrap_or_else(|| DEFAULT_AGENT_NAME.into());
        (self.working.borrow().clone(), active)
    }
}

impl Alexy {
    /// The original `_build_agents_page`.
    fn build_agents_page(self: &Rc<Self>) -> (adw::PreferencesPage, Rc<Agents>) {
        let page = adw::PreferencesPage::new();
        let config = self.config.borrow().clone();
        let names_all = config.agent_names();
        let start_active = if names_all.contains(&config.active_agent) {
            config.active_agent.clone()
        } else {
            DEFAULT_AGENT_NAME.to_owned()
        };
        let select_group = adw::PreferencesGroup::new();
        select_group.set_title(&tr("Agents"));
        select_group.set_description(Some(&tr(
            "Agents are named master-prompt profiles. Select one to use for new conversations, or create your own. The built-in agents cannot be edited.",
        )));
        page.add(&select_group);
        let combo = adw::ComboRow::new();
        combo.set_title(&tr("Active Agent"));
        combo.set_subtitle(&tr("Applied to new conversations"));
        select_group.add(&combo);
        let new_row = adw::ActionRow::new();
        new_row.set_title(&tr("Create New Agent"));
        new_row.set_subtitle(&tr("Start from a copy of the default prompt"));
        let new_btn = gtk::Button::with_label(&tr("New Agent"));
        new_btn.set_valign(gtk::Align::Center);
        new_btn.add_css_class("suggested-action");
        new_row.add_suffix(&new_btn);
        new_row.set_activatable_widget(Some(&new_btn));
        select_group.add(&new_row);
        let editor_group = adw::PreferencesGroup::new();
        page.add(&editor_group);
        let editor_box = gtk::Box::new(gtk::Orientation::Vertical, 8);
        editor_group.add(&editor_box);

        let agents = Rc::new(Agents {
            alexy: Rc::downgrade(self),
            working: RefCell::new(config.user_agents.clone()),
            combo: combo.clone(),
            editor_box,
            current_name: RefCell::new(start_active.clone()),
            buffer: RefCell::default(),
            loading: Cell::new(false),
            default_prompt: config.default_system_prompt.clone(),
        });
        let weak = Rc::downgrade(&agents);
        combo.connect_selected_notify(move |row| {
            let Some(agents) = weak.upgrade() else {
                return;
            };
            if agents.loading.get() {
                return;
            }
            agents.commit_editor();
            let names = agents.names();
            if let Some(name) = names.get(row.selected() as usize) {
                guard("agent", || agents.build_editor(name));
            }
        });
        let weak = Rc::downgrade(&agents);
        new_btn.connect_clicked(move |_| {
            if let Some(agents) = weak.upgrade() {
                guard("new agent", || agents.on_new_agent());
            }
        });
        agents.refresh_combo(&start_active);
        (page, agents)
    }

    /// `_refresh_ollama_models`.
    fn refresh_ollama_models(&self, combo: &adw::ComboRow) {
        let parsed = ollama_models();
        let mut labels = Vec::new();
        let mut names = Vec::new();
        if parsed.is_empty() {
            labels.push(if ollama_installed() {
                tr("No Models Downloaded")
            } else {
                tr("Ollama Not Installed")
            });
            names.push(String::new());
        } else {
            for (name, size) in parsed {
                labels.push(format!("{name} ({size})"));
                names.push(name);
            }
        }
        combo.set_model(Some(&string_list(&labels)));
        let local = self.config.borrow().local_model.clone();
        let index = names.iter().position(|name| *name == local).unwrap_or(0);
        self.dynamic_models.replace(names);
        combo.set_selected(index as u32);
    }

    /// `launch_in_app_process`.
    #[allow(clippy::too_many_arguments)]
    fn launch_in_app_process(
        &self,
        title: &str,
        command: &str,
        is_ollama: bool,
        initial: Option<&str>,
        on_close: Option<OnClose>,
        sudo: bool,
        model: Option<&str>,
    ) {
        progress::launch(
            self.window_or_root().as_ref(),
            title,
            command,
            is_ollama,
            initial,
            on_close,
            if sudo { self.host } else { None },
            model,
        )
        .present();
    }

    /// `on_pull_ollama_clicked`: `callback` hears how it went.
    pub fn on_pull_ollama_clicked(
        self: &Rc<Self>,
        model: &str,
        callback: Option<Box<dyn FnOnce(bool)>>,
        combo: Option<adw::ComboRow>,
    ) {
        if model.is_empty() {
            self.add_message_bubble(
                "assistant",
                &json!(tr("Please enter a model name to pull.")),
            );
            return;
        }
        let weak = Rc::downgrade(self);
        self.launch_in_app_process(
            &tr("Downloading {}").replacen("{}", model, 1),
            &format!("ollama pull {model}"),
            true,
            Some(&tr("Initiating download...")),
            Some(Box::new(move |success| {
                if success && let (Some(combo), Some(alexy)) = (&combo, weak.upgrade()) {
                    alexy.refresh_ollama_models(combo);
                }
                // The original told only a success; a failure is told too,
                // so a waiting reply does not stay locked.
                if let Some(callback) = callback {
                    callback(success);
                }
            })),
            false,
            Some(model),
        );
    }

    /// `on_remove_ollama_clicked`.
    fn on_remove_ollama_clicked(self: &Rc<Self>, combo: &adw::ComboRow) {
        let index = combo.selected();
        let models = self.dynamic_models.borrow().clone();
        if index == gtk::INVALID_LIST_POSITION || index as usize >= models.len() {
            return;
        }
        let model = models[index as usize].clone();
        if model.is_empty() {
            self.add_message_bubble(
                "assistant",
                &json!(tr("No downloaded model selected to remove.")),
            );
            return;
        }
        let weak = Rc::downgrade(self);
        let combo = combo.clone();
        let removed = model.clone();
        self.launch_in_app_process(
            &format!("Removing {model}"),
            &format!("ollama rm {model}"),
            true,
            Some(&tr("Deleting model files...")),
            Some(Box::new(move |success| {
                if success && let Some(alexy) = weak.upgrade() {
                    alexy.refresh_ollama_models(&combo);
                    alexy.add_message_bubble(
                        "assistant",
                        &json!(
                            tr("Model {} has been successfully removed.")
                                .replacen("{}", &removed, 1)
                        ),
                    );
                }
            })),
            false,
            None,
        );
    }

    /// `on_ollama_install_clicked`: the password first, then Ollama's script.
    pub fn on_ollama_install_clicked(self: &Rc<Self>, callback: Option<Box<dyn FnOnce(bool)>>) {
        let Some(host) = self.host else {
            self.add_message_bubble(
                "assistant",
                &json!(tr("Error: Sudo manager not available.")),
            );
            return;
        };
        let callback = Rc::new(RefCell::new(callback));
        if !host.sudo_has_password() {
            let weak = Rc::downgrade(self);
            let retry = callback.clone();
            let cancelled = callback.clone();
            self.prompt_for_password_dialog(
                Box::new(move || {
                    if let Some(alexy) = weak.upgrade() {
                        alexy.on_ollama_install_clicked(retry.borrow_mut().take());
                    }
                }),
                &tr("Please enter your password to install Ollama via system privileges."),
                Some(Box::new(move || {
                    if let Some(callback) = cancelled.borrow_mut().take() {
                        callback(false);
                    }
                })),
            );
            return;
        }
        self.launch_in_app_process(
            &tr("Installing Ollama"),
            "curl -fsSL https://ollama.com/install.sh | sh",
            false,
            Some(&tr("Downloading and installing Ollama daemon...")),
            Some(Box::new(move |success| {
                if let Some(callback) = callback.borrow_mut().take() {
                    callback(success);
                }
            })),
            true,
            None,
        );
    }

    /// `_close_settings_sidebar`: pending settings applied, sidebar closed.
    pub fn close_settings_sidebar(&self) {
        if let Some(apply) = self.settings_apply.take() {
            apply();
        }
        self.settings_revealer.set_reveal_child(false);
        self.settings_scrim.set_visible(false);
    }

    /// `on_settings_clicked`: build the sidebar and slide it in (or close it).
    pub fn on_settings_clicked(self: &Rc<Self>) {
        if self.settings_revealer.reveals_child() {
            self.close_settings_sidebar();
            return;
        }
        if self.conv_toggle_btn.is_active() {
            self.conv_toggle_btn.set_active(false);
        }
        let config = self.config.borrow().clone();
        let stack = adw::ViewStack::new();
        let page_llm = adw::PreferencesPage::new();
        stack.add_titled_with_icon(
            &page_llm,
            Some("assistant"),
            &tr("Assistant"),
            "preferences-system-symbolic",
        );
        let (agents_page, agents) = self.build_agents_page();
        stack.add_titled_with_icon(
            &agents_page,
            Some("agents"),
            &tr("Agents"),
            "system-users-symbolic",
        );

        let safety_group = adw::PreferencesGroup::new();
        safety_group.set_title(&tr("General Settings"));
        page_llm.add(&safety_group);
        let auto_exec_row = adw::SwitchRow::new();
        auto_exec_row.set_title(&tr("Auto-Execute Commands"));
        auto_exec_row.set_subtitle(&tr("Allow AI to silently execute bash commands without prompting for permission first. Leave checked for a continuous experience."));
        auto_exec_row.set_active(config.auto_execute_commands);
        safety_group.add(&auto_exec_row);
        let compact_screen_row = adw::SwitchRow::new();
        compact_screen_row.set_title(&tr("Compact Mode Screen Awareness"));
        compact_screen_row.set_subtitle(&tr("Automatically capture and send a screenshot to the AI when using compact voice mode (Hey Alexy)."));
        compact_screen_row.set_active(config.compact_screen_awareness);
        safety_group.add(&compact_screen_row);

        let general_group = adw::PreferencesGroup::new();
        general_group.set_title(&tr("Backend Type"));
        general_group.set_description(Some(&tr("Configure how Alexy connects to models.")));
        page_llm.add(&general_group);
        let backend_row = adw::ComboRow::new();
        backend_row.set_title(&tr("Backend Type"));
        backend_row.set_model(Some(&string_list(&[
            tr("Direct API (Online)"),
            tr("Local AI (Ollama)"),
            tr("Local AI (Endpoint)"),
        ])));
        match config.backend.as_str() {
            "direct" => backend_row.set_selected(0),
            "local" => backend_row.set_selected(1),
            "endpoint" => backend_row.set_selected(2),
            _ => {}
        }
        general_group.add(&backend_row);

        let direct_group = adw::PreferencesGroup::new();
        direct_group.set_description(Some(&tr(
            "Uses urllib to connect directly to Qwen or OpenAI compatible APIs.",
        )));
        page_llm.add(&direct_group);
        let api_key_entry = adw::PasswordEntryRow::new();
        api_key_entry.set_title(&tr("API Key"));
        api_key_entry.set_text(&config.api_key);
        direct_group.add(&api_key_entry);
        let api_url_entry = adw::EntryRow::new();
        api_url_entry.set_title(&tr("API URL"));
        api_url_entry.set_text(&config.api_url);
        direct_group.add(&api_url_entry);
        let model_entry = adw::EntryRow::new();
        model_entry.set_title(&tr("Model"));
        model_entry.set_text(&config.model);
        direct_group.add(&model_entry);

        let endpoint_group = adw::PreferencesGroup::new();
        endpoint_group.set_title(&tr("Local AI (Endpoint)"));
        endpoint_group.set_description(Some(&tr("Connects to a local OpenAI-compatible server via its endpoint URL (e.g. http://localhost:6767/v1). The model is detected automatically — no API key, model name or Ollama required.")));
        page_llm.add(&endpoint_group);
        let endpoint_url_entry = adw::EntryRow::new();
        endpoint_url_entry.set_title(&tr("Endpoint URL"));
        endpoint_url_entry.set_text(&config.endpoint_url);
        endpoint_group.add(&endpoint_url_entry);

        let local_group = adw::PreferencesGroup::new();
        local_group.set_title(&tr("Local AI"));
        local_group.set_description(Some(&tr(
            "Uses Ollama daemon sequentially running on localhost:11434.",
        )));
        page_llm.add(&local_group);
        let local_model_row = adw::ComboRow::new();
        local_model_row.set_title(&tr("Select Downloaded Model"));
        self.refresh_ollama_models(&local_model_row);
        local_group.add(&local_model_row);
        let remove_row = adw::ActionRow::new();
        remove_row.set_title(&tr("Delete from Disk"));
        let remove_btn = gtk::Button::with_label(&tr("Remove Model"));
        remove_btn.set_valign(gtk::Align::Center);
        remove_btn.add_css_class("destructive-action");
        {
            let weak = Rc::downgrade(self);
            let combo = local_model_row.clone();
            remove_btn.connect_clicked(move |_| {
                if let Some(alexy) = weak.upgrade() {
                    guard("remove model", || alexy.on_remove_ollama_clicked(&combo));
                }
            });
        }
        remove_row.add_suffix(&remove_btn);
        remove_row.set_activatable_widget(Some(&remove_btn));
        local_group.add(&remove_row);
        local_group.add(&self.ollama_manage_row(&local_model_row));

        let pull_group = adw::PreferencesGroup::new();
        pull_group.set_title(&tr("Download Model"));
        page_llm.add(&pull_group);
        let popular_row = adw::ComboRow::new();
        popular_row.set_title(&tr("Popular Models"));
        popular_row.set_model(Some(&string_list(
            &POPULAR_NAMES
                .iter()
                .map(|name| name.to_string())
                .collect::<Vec<_>>(),
        )));
        pull_group.add(&popular_row);
        let pull_entry = adw::EntryRow::new();
        pull_entry.set_title(&tr("Model Name (e.g. qwen3.5)"));
        pull_entry.set_text(REAL_POPULAR_NAMES[0]);
        pull_group.add(&pull_entry);
        let pull_row = adw::ActionRow::new();
        pull_row.set_title(&tr("Start Download"));
        let pull_btn = gtk::Button::with_label(&tr("Pull from Registry"));
        pull_btn.set_valign(gtk::Align::Center);
        pull_btn.add_css_class("suggested-action");
        {
            let weak = Rc::downgrade(self);
            let (entry, combo) = (pull_entry.clone(), local_model_row.clone());
            pull_btn.connect_clicked(move |_| {
                if let Some(alexy) = weak.upgrade() {
                    let model = entry.text().to_string();
                    guard("pull", || {
                        alexy.on_pull_ollama_clicked(&model, None, Some(combo.clone()))
                    });
                }
            });
        }
        pull_row.add_suffix(&pull_btn);
        pull_row.set_activatable_widget(Some(&pull_btn));
        pull_group.add(&pull_row);
        let downloaded: Vec<String> = self
            .dynamic_models
            .borrow()
            .iter()
            .filter(|model| !model.is_empty())
            .map(|model| model.split(':').next().unwrap_or_default().to_owned())
            .collect();
        let update_pull = {
            let (entry, button) = (pull_entry.clone(), pull_btn.clone());
            Rc::new(move || {
                let model = crate::conversations::python_strip(&entry.text()).to_owned();
                if downloaded.contains(&model) {
                    button.set_sensitive(false);
                    button.set_label(&tr("Already Downloaded"));
                } else {
                    button.set_sensitive(true);
                    button.set_label(&tr("Pull from Registry"));
                }
            })
        };
        {
            let (entry, update) = (pull_entry.clone(), update_pull.clone());
            popular_row.connect_selected_notify(move |row| {
                let index = row.selected();
                if index != gtk::INVALID_LIST_POSITION
                    && (index as usize) < REAL_POPULAR_NAMES.len()
                {
                    entry.set_text(REAL_POPULAR_NAMES[index as usize]);
                }
                update();
            });
        }
        {
            let update = update_pull.clone();
            pull_entry.connect_changed(move |_| update());
        }
        update_pull();

        let sync_backend = {
            let (backend_row, direct, local, pull, endpoint) = (
                backend_row.clone(),
                direct_group.clone(),
                local_group.clone(),
                pull_group.clone(),
                endpoint_group.clone(),
            );
            move || {
                let index = backend_row.selected();
                direct.set_visible(index == 0);
                local.set_visible(index == 1);
                pull.set_visible(index == 1);
                endpoint.set_visible(index == 2);
            }
        };
        let sync = Rc::new(sync_backend);
        {
            let sync = sync.clone();
            backend_row.connect_selected_notify(move |_| sync());
        }
        sync();

        // Speech
        let page_speech = adw::PreferencesPage::new();
        stack.add_titled_with_icon(
            &page_speech,
            Some("speech"),
            &tr("Speech"),
            "audio-input-microphone-symbolic",
        );
        let engine_group = adw::PreferencesGroup::new();
        engine_group.set_title(&tr("Voice-to-Text Engine"));
        page_speech.add(&engine_group);
        let stt_backend_row = adw::ComboRow::new();
        stt_backend_row.set_title(&tr("STT Backend"));
        let vosk_available = crate::voice::python_has("vosk");
        let vosk_label = if vosk_available {
            tr("Vosk (Lightweight)")
        } else {
            tr("Vosk (Not Installed)")
        };
        stt_backend_row.set_model(Some(&string_list(&[
            tr("OpenAI Whisper (Recommended)"),
            vosk_label,
        ])));
        stt_backend_row.set_selected(if config.stt_backend == "whisper" {
            0
        } else {
            1
        });
        engine_group.add(&stt_backend_row);

        let whisper_group = adw::PreferencesGroup::new();
        whisper_group.set_title(&tr("Whisper Settings"));
        whisper_group.set_description(Some(&tr("OpenAI Whisper provides high-accuracy offline transcription. Model is auto-downloaded on first use.")));
        page_speech.add(&whisper_group);
        let whisper_model_row = adw::ComboRow::new();
        whisper_model_row.set_title(&tr("Model Size"));
        whisper_model_row.set_model(Some(&string_list(&[
            tr("Tiny (~39 MB, fastest)"),
            tr("Base (~74 MB)"),
            tr("Small (~461 MB, recommended)"),
            tr("Medium (~1.5 GB, most accurate)"),
        ])));
        let whisper_selected = WHISPER_MODELS
            .iter()
            .position(|model| *model == config.whisper_model)
            .unwrap_or(1);
        whisper_model_row.set_selected(whisper_selected as u32);
        whisper_group.add(&whisper_model_row);

        let vosk_group = adw::PreferencesGroup::new();
        vosk_group.set_title(&tr("Vosk Settings"));
        vosk_group.set_description(Some(&tr(
            "Vosk provides lightweight, streaming offline transcription.",
        )));
        page_speech.add(&vosk_group);
        if !vosk_available {
            vosk_group.add(&self.vosk_install_row(&stt_backend_row));
        }
        let voice_lang_row = adw::ComboRow::new();
        voice_lang_row.set_title(&tr("Vosk Language Model"));
        let languages = vosk_languages();
        voice_lang_row.set_model(Some(&string_list(
            &languages
                .iter()
                .map(|(id, name)| format!("{name} ({id})"))
                .collect::<Vec<_>>(),
        )));
        let lang_selected = languages
            .iter()
            .position(|(id, _)| *id == config.vosk_lang)
            .unwrap_or(0);
        voice_lang_row.set_selected(lang_selected as u32);
        vosk_group.add(&voice_lang_row);
        let sync_stt = {
            let (row, whisper, vosk) = (
                stt_backend_row.clone(),
                whisper_group.clone(),
                vosk_group.clone(),
            );
            Rc::new(move || {
                let is_whisper = row.selected() == 0;
                whisper.set_visible(is_whisper);
                vosk.set_visible(!is_whisper);
            })
        };
        {
            let sync = sync_stt.clone();
            stt_backend_row.connect_selected_notify(move |_| sync());
        }
        sync_stt();

        let hey_group = adw::PreferencesGroup::new();
        hey_group.set_title(&tr("Hey Alexy Wake Word"));
        hey_group.set_description(Some(&tr("Continuously listens for 'Hey Alexy' to activate the assistant. Uses openWakeWord for lightweight wake word detection.")));
        page_speech.add(&hey_group);
        let hey_row = adw::SwitchRow::new();
        hey_row.set_title(&tr("Enable \"Hey Alexy\""));
        hey_row.set_active(config.hey_linux_enabled);
        {
            let weak = Rc::downgrade(self);
            hey_row.connect_active_notify(move |row| {
                if let Some(alexy) = weak.upgrade() {
                    let active = row.is_active();
                    guard("hey alexy", || alexy.on_hey_linux_toggled(active));
                }
            });
        }
        hey_group.add(&hey_row);
        let vc_group = adw::PreferencesGroup::new();
        vc_group.set_title(&tr("Voice Correction"));
        vc_group.set_description(Some(&tr("Use an LLM to automatically fix transcribing errors. Note: This currently only functions when an online model (Direct API) is actively configured.")));
        page_speech.add(&vc_group);
        let direct_vc_row = adw::SwitchRow::new();
        direct_vc_row.set_title(&tr("Enable for Direct API"));
        direct_vc_row.set_active(config.voice_correction_direct);
        vc_group.add(&direct_vc_row);

        // Theme
        let page_theme = adw::PreferencesPage::new();
        stack.add_titled_with_icon(
            &page_theme,
            Some("theme"),
            &tr("Theme"),
            "applications-graphics-symbolic",
        );
        let theme_group = adw::PreferencesGroup::new();
        theme_group.set_title(&tr("Appearance"));
        theme_group.set_description(Some(&tr(
            "Select a theme to customize the look of the AI assistant.",
        )));
        page_theme.add(&theme_group);
        let themes = theme::discover();
        let theme_ids: Vec<String> = themes.iter().map(|theme| theme.id.clone()).collect();
        let current_theme = themes
            .iter()
            .position(|theme| theme.id == config.theme)
            .unwrap_or(0);
        let theme_row = adw::ComboRow::new();
        theme_row.set_title(&tr("Theme"));
        theme_row.set_model(Some(&string_list(
            &themes
                .iter()
                .map(|theme| theme.name.clone())
                .collect::<Vec<_>>(),
        )));
        theme_row.set_selected(current_theme as u32);
        theme_group.add(&theme_row);
        let preview_group = adw::PreferencesGroup::new();
        preview_group.set_title(&tr("Preview"));
        page_theme.add(&preview_group);
        let preview_box = gtk::Box::new(gtk::Orientation::Vertical, 12);
        preview_box.set_margin_top(12);
        preview_box.set_margin_bottom(12);
        preview_box.set_halign(gtk::Align::Center);
        let preview_avatar = gtk::Image::new();
        preview_avatar.set_pixel_size(64);
        preview_box.append(&preview_avatar);
        let preview_desc = gtk::Label::new(None);
        preview_desc.add_css_class("dim-label");
        preview_desc.set_wrap(true);
        preview_desc.set_halign(gtk::Align::Center);
        preview_box.append(&preview_desc);
        let preview_author = gtk::Label::new(None);
        preview_author.add_css_class("caption");
        preview_author.add_css_class("dim-label");
        preview_author.set_halign(gtk::Align::Center);
        preview_box.append(&preview_author);
        preview_group.add(&preview_box);
        let update_preview = Rc::new(move |index: usize| {
            let Some(theme) = themes.get(index) else {
                return;
            };
            let avatar = theme.path.join("assistant-avatar.svg");
            if avatar.is_file() {
                preview_avatar.set_from_file(Some(&avatar));
            } else {
                preview_avatar.set_icon_name(Some("applications-graphics-symbolic"));
            }
            preview_desc.set_label(&theme.description);
            preview_author.set_label(&tr("by {}").replacen("{}", &theme.author, 1));
        });
        update_preview(current_theme);
        {
            let update = update_preview.clone();
            theme_row.connect_selected_notify(move |row| update(row.selected() as usize));
        }
        let info_group = adw::PreferencesGroup::new();
        page_theme.add(&info_group);
        let info_label = gtk::Label::new(Some(
            &tr("Custom themes can be installed to:\n{}").replacen(
                "{}",
                &config::user_themes_dir().to_string_lossy(),
                1,
            ),
        ));
        info_label.set_halign(gtk::Align::Center);
        info_label.add_css_class("dim-label");
        info_label.add_css_class("caption");
        info_label.set_wrap(true);
        info_group.add(&info_label);

        // Applied when the sidebar closes.
        let weak = Rc::downgrade(self);
        let apply = move || {
            let Some(alexy) = weak.upgrade() else {
                return;
            };
            let (agents_list, active) = agents.state();
            {
                let mut config = alexy.config.borrow_mut();
                config.user_agents = agents_list;
                config.active_agent = active;
                if !config.agent_names().contains(&config.active_agent) {
                    config.active_agent = DEFAULT_AGENT_NAME.into();
                }
                config.apply_active_agent();
            }
            if alexy.history_len() <= 1 {
                alexy.reset_history();
            }
            let new_backend = match backend_row.selected() {
                0 => "direct",
                1 => "local",
                2 => "endpoint",
                _ => "",
            };
            let old_backend = alexy.config.borrow().backend.clone();
            if !new_backend.is_empty() && new_backend != old_backend {
                alexy.save_conversation();
                alexy.config.borrow_mut().backend = new_backend.into();
                alexy
                    .conversation_id
                    .replace(glib::uuid_string_random().to_string());
                alexy.conv_created.replace(None);
                alexy.reset_history();
                alexy.clear_chat_ui();
                alexy.add_message_bubble(
                    "assistant",
                    &json!(tr("Hello! I am Alexy. How can I help you today?")),
                );
                alexy.add_message_bubble(
                    "assistant",
                    &json!(tr("Switched backend mode. New conversation started.")),
                );
            }
            {
                let mut config = alexy.config.borrow_mut();
                config.api_key = api_key_entry.text().to_string();
                config.api_url = api_url_entry.text().to_string();
                config.model = model_entry.text().to_string();
                let new_endpoint =
                    crate::conversations::python_strip(&endpoint_url_entry.text()).to_owned();
                if !new_endpoint.is_empty() && new_endpoint != config.endpoint_url {
                    config.endpoint_url = new_endpoint;
                    config.endpoint_model = String::new();
                }
                let models = alexy.dynamic_models.borrow();
                let selected = local_model_row.selected() as usize;
                if !models.is_empty() && selected < models.len() && !models[selected].is_empty() {
                    config.local_model = models[selected].clone();
                }
                let voice = voice_lang_row.selected();
                let languages = vosk_languages();
                if voice != gtk::INVALID_LIST_POSITION && (voice as usize) < languages.len() {
                    config.vosk_lang = languages[voice as usize].0.into();
                }
                config.stt_backend = if stt_backend_row.selected() == 0 {
                    "whisper"
                } else {
                    "vosk"
                }
                .into();
                let whisper = whisper_model_row.selected() as usize;
                if whisper < WHISPER_MODELS.len() {
                    config.whisper_model = WHISPER_MODELS[whisper].into();
                }
            }
            alexy.check_stt_availability();
            {
                let mut config = alexy.config.borrow_mut();
                config.voice_correction_direct = direct_vc_row.is_active();
                config.auto_execute_commands = auto_exec_row.is_active();
                config.compact_screen_awareness = compact_screen_row.is_active();
            }
            let selected_theme = theme_row.selected() as usize;
            if let Some(new_theme) = theme_ids.get(selected_theme)
                && *new_theme != alexy.config.borrow().theme
            {
                alexy.config.borrow_mut().theme = new_theme.clone();
                alexy.load_theme();
                if alexy.alexy_icon_path.is_file() {
                    alexy
                        .header_icon
                        .set_from_file(Some(&alexy.alexy_icon_path));
                } else {
                    alexy.header_icon.set_icon_name(Some("system-run-symbolic"));
                }
                match alexy.theme_svg("microphone-icon.svg") {
                    Some(svg) => alexy.stt_icon.set_from_file(Some(&svg)),
                    None => alexy
                        .stt_icon
                        .set_icon_name(Some("audio-input-microphone-symbolic")),
                }
            }
            alexy.config.borrow().save();
            alexy.update_subtitle();
        };
        self.settings_apply.replace(Some(Box::new(apply)));

        let sidebar = gtk::Box::new(gtk::Orientation::Vertical, 0);
        sidebar.add_css_class("settings-sidebar");
        sidebar.set_size_request(400, -1);
        let header = gtk::Box::new(gtk::Orientation::Horizontal, 8);
        header.add_css_class("settings-sidebar-header");
        let title = gtk::Label::new(Some(&tr("Settings")));
        title.add_css_class("title-2");
        title.set_hexpand(true);
        title.set_halign(gtk::Align::Start);
        header.append(&title);
        let close = gtk::Button::new();
        set_button_icon(
            &close,
            &["window-close-symbolic", "window-close"],
            Some("\u{2715}"),
        );
        close.add_css_class("flat");
        close.add_css_class("circular");
        close.set_valign(gtk::Align::Center);
        close.set_tooltip_text(Some(&tr("Close")));
        let weak = Rc::downgrade(self);
        close.connect_clicked(move |_| {
            if let Some(alexy) = weak.upgrade() {
                guard("close settings", || alexy.close_settings_sidebar());
            }
        });
        header.append(&close);
        sidebar.append(&header);
        let switcher = adw::ViewSwitcher::new();
        switcher.set_stack(Some(&stack));
        switcher.set_policy(adw::ViewSwitcherPolicy::Wide);
        switcher.set_halign(gtk::Align::Center);
        let switcher_bar = gtk::Box::new(gtk::Orientation::Horizontal, 0);
        switcher_bar.add_css_class("settings-switcher-bar");
        switcher_bar.set_halign(gtk::Align::Center);
        switcher_bar.append(&switcher);
        sidebar.append(&switcher_bar);
        stack.set_vexpand(true);
        sidebar.append(&stack);
        self.settings_revealer.set_child(Some(&sidebar));
        self.translate(&sidebar);
        self.settings_scrim.set_visible(true);
        self.settings_revealer.set_reveal_child(true);
    }

    /// The Ollama install/uninstall row of the Local AI group.
    fn ollama_manage_row(self: &Rc<Self>, local_model_row: &adw::ComboRow) -> adw::ActionRow {
        let row = adw::ActionRow::new();
        let button = gtk::Button::new();
        button.set_valign(gtk::Align::Center);
        let state = Rc::new(Cell::new(ollama_installed()));
        let show = {
            let (row, button) = (row.clone(), button.clone());
            move |installed: bool| {
                if installed {
                    row.set_title(&tr("Ollama is installed"));
                    row.set_subtitle(&tr(
                        "Remove the Ollama daemon and its dependencies from your system.",
                    ));
                    button.set_label(&tr("Uninstall Ollama"));
                    button.remove_css_class("suggested-action");
                    button.add_css_class("destructive-action");
                } else {
                    row.set_title(&tr("Ollama is not installed"));
                    row.set_subtitle(&tr("Install the Ollama daemon to use Local AI."));
                    button.set_label(&tr("Install Ollama"));
                    button.remove_css_class("destructive-action");
                    button.add_css_class("suggested-action");
                }
            }
        };
        show(state.get());
        let show = Rc::new(show);
        let weak = Rc::downgrade(self);
        let combo = local_model_row.clone();
        let titled = row.downgrade();
        button.connect_clicked(move |_| {
            let Some(alexy) = weak.upgrade() else {
                return;
            };
            let installed = state.get();
            let (state, show, combo, weak, titled) = (
                state.clone(),
                show.clone(),
                combo.clone(),
                weak.clone(),
                titled.clone(),
            );
            let run = move || {
                let Some(alexy) = weak.upgrade() else {
                    return;
                };
                let (title, command, initial) = if installed {
                    (tr("Uninstalling Ollama"), UNINSTALL_OLLAMA, None)
                } else {
                    (
                        tr("Installing Ollama"),
                        INSTALL_OLLAMA,
                        Some(tr("Downloading and installing Ollama daemon...")),
                    )
                };
                let after = {
                    let weak = weak.clone();
                    move |success: bool| {
                        if success {
                            state.set(!installed);
                            show(!installed);
                            if installed && let Some(row) = titled.upgrade() {
                                row.set_title(&tr("Ollama has been uninstalled"));
                            }
                            if let Some(alexy) = weak.upgrade() {
                                alexy.refresh_ollama_models(&combo);
                            }
                        }
                    }
                };
                alexy.launch_in_app_process(
                    &title,
                    command,
                    false,
                    initial.as_deref(),
                    Some(Box::new(after)),
                    true,
                    None,
                );
            };
            let has_password = alexy.host.is_some_and(|host| host.sudo_has_password());
            if has_password {
                run();
            } else {
                let message = if installed {
                    tr("Please enter your password to uninstall Ollama.")
                } else {
                    tr("Please enter your password to install Ollama via system privileges.")
                };
                alexy.prompt_for_password_dialog(Box::new(run), &message, None);
            }
        });
        row.add_suffix(&button);
        row.set_activatable_widget(Some(&button));
        row
    }

    /// The "Vosk is not installed" row with its install button.
    fn vosk_install_row(self: &Rc<Self>, stt_backend_row: &adw::ComboRow) -> adw::ActionRow {
        let row = adw::ActionRow::new();
        row.set_title(&tr("Vosk is not installed"));
        row.set_subtitle(&tr("Install python-vosk to use the Vosk backend."));
        let button = gtk::Button::with_label(&tr("Install python-vosk"));
        button.set_valign(gtk::Align::Center);
        button.add_css_class("suggested-action");
        let weak = Rc::downgrade(self);
        let (row_weak, button_weak, combo) =
            (row.downgrade(), button.downgrade(), stt_backend_row.clone());
        button.connect_clicked(move |_| {
            let Some(alexy) = weak.upgrade() else {
                return;
            };
            let (weak, row_weak, button_weak, combo) = (
                weak.clone(),
                row_weak.clone(),
                button_weak.clone(),
                combo.clone(),
            );
            let install = move || {
                let Some(alexy) = weak.upgrade() else {
                    return;
                };
                let weak_inner = weak.clone();
                alexy.launch_in_app_process(
                    &tr("Installing python-vosk"),
                    INSTALL_VOSK,
                    false,
                    None,
                    Some(Box::new(move |success| {
                        if !success {
                            return;
                        }
                        if let Some(row) = row_weak.upgrade() {
                            row.set_title(&tr("Vosk installed successfully!"));
                            row.set_subtitle(&tr("Vosk backend is now available."));
                        }
                        combo.set_model(Some(&string_list(&[
                            tr("OpenAI Whisper (Recommended)"),
                            tr("Vosk (Lightweight)"),
                        ])));
                        if let Some(alexy) = weak_inner.upgrade() {
                            alexy.check_stt_availability();
                        }
                        if let Some(button) = button_weak.upgrade() {
                            button.set_sensitive(false);
                        }
                    })),
                    true,
                    None,
                );
            };
            if alexy.host.is_some_and(|host| host.sudo_has_password()) {
                install();
            } else {
                alexy.prompt_for_password_dialog(
                    Box::new(install),
                    &tr("Please enter your password to install python-vosk."),
                    None,
                );
            }
        });
        row.add_suffix(&button);
        row.set_activatable_widget(Some(&button));
        row
    }
}
