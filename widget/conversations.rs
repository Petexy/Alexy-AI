//! Saved conversations (`~/.config/linexin/conversations/<id>.json`) and the
//! message-content helpers: the original's `_save_conversation`,
//! `_load_conversation`, `_list_conversations`, `_delete_conversation`,
//! `_rename_conversation`, `_generate_title`, `_extract_text_from_content`,
//! `_strip_system_instructions` and `_extract_images_from_content`.

use crate::config::{self, DEFAULT_AGENT_NAME};
use crate::pyjson;
use crate::ui::tr;
use serde_json::{Map, Value, json};
use std::path::PathBuf;

/// What Screen Awareness puts before the user's words (hidden in the chat).
pub const SCREEN_AWARENESS_PREFIX: &str = "[A screenshot of my current screen is attached. IMPORTANT: If my question is NOT about the screen content, do NOT describe, mention, reference, or acknowledge the screenshot in any way — just answer my question directly as if no screenshot was provided. Only use the screenshot if my question is specifically about what is on screen. LANGUAGE RULE: The language visible in the screenshot must NEVER influence your reply language. Always reply in the language of my text message, regardless of what language appears on screen.]\n\n";

/// `_get_conversations_dir`: created on demand.
pub fn dir() -> PathBuf {
    let dir = config::conversations_dir();
    let _ = std::fs::create_dir_all(&dir);
    dir
}

fn file(id: &str) -> PathBuf {
    dir().join(format!("{id}.json"))
}

/// `datetime.now().isoformat()`.
pub fn now_iso() -> String {
    let now = gtk::glib::DateTime::now_local().ok();
    match now {
        Some(now) => {
            let micro = now.microsecond();
            let base = now
                .format("%Y-%m-%dT%H:%M:%S")
                .map(|text| text.to_string())
                .unwrap_or_default();
            if micro == 0 {
                base
            } else {
                format!("{base}.{micro:06}")
            }
        }
        None => String::new(),
    }
}

/// `datetime.fromisoformat(updated).strftime("%Y-%m-%d %H:%M")`, or the text.
pub fn display_time(updated: &str) -> String {
    let date = updated.get(..10);
    let time = updated.get(11..16);
    let well_formed = updated.len() >= 16
        && updated
            .as_bytes()
            .get(10)
            .is_some_and(|c| *c == b'T' || *c == b' ')
        && date.is_some_and(|d| {
            d.len() == 10
                && d.chars().enumerate().all(|(i, c)| {
                    if i == 4 || i == 7 {
                        c == '-'
                    } else {
                        c.is_ascii_digit()
                    }
                })
        })
        && time.is_some_and(|t| {
            t.chars()
                .enumerate()
                .all(|(i, c)| if i == 2 { c == ':' } else { c.is_ascii_digit() })
        });
    match (well_formed, date, time) {
        (true, Some(date), Some(time)) => format!("{date} {time}"),
        _ => updated.to_owned(),
    }
}

/// `_extract_text_from_content`.
pub fn extract_text(content: &Value) -> String {
    match content {
        Value::String(text) => text.clone(),
        Value::Array(items) => items
            .iter()
            .filter_map(Value::as_object)
            .filter(|item| item.get("type").and_then(Value::as_str) == Some("text"))
            .map(|item| item.get("text").map(config::python_str).unwrap_or_default())
            .collect::<Vec<_>>()
            .join(" "),
        other => config::python_str(other),
    }
}

/// `_strip_system_instructions`: the Screen Awareness preamble removed.
pub fn strip_system_instructions(content: &Value) -> Value {
    match content {
        Value::Array(items) => Value::Array(
            items
                .iter()
                .filter_map(|item| {
                    let text = item
                        .as_object()
                        .filter(|item| item.get("type").and_then(Value::as_str) == Some("text"))
                        .and_then(|item| item.get("text"))
                        .and_then(Value::as_str);
                    match text.and_then(|text| text.strip_prefix(SCREEN_AWARENESS_PREFIX)) {
                        Some("") => None,
                        Some(clean) => Some(json!({"type": "text", "text": clean})),
                        None => Some(item.clone()),
                    }
                })
                .collect(),
        ),
        Value::String(text) => match text.strip_prefix(SCREEN_AWARENESS_PREFIX) {
            Some(clean) => Value::String(clean.to_owned()),
            None => content.clone(),
        },
        other => other.clone(),
    }
}

/// `_extract_images_from_content`: the data URLs of the images.
pub fn extract_images(content: &Value) -> Vec<String> {
    let Value::Array(items) = content else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(Value::as_object)
        .filter(|item| item.get("type").and_then(Value::as_str) == Some("image_url"))
        .filter_map(|item| {
            item.get("image_url")
                .and_then(|url| url.get("url"))
                .and_then(Value::as_str)
                .filter(|url| !url.is_empty())
                .map(str::to_owned)
        })
        .collect()
}

/// `_generate_title`: the first user message, at most 50 characters.
pub fn generate_title(history: &[Value]) -> String {
    for message in history {
        if message.get("role").and_then(Value::as_str) == Some("user") {
            let content = message.get("content").cloned().unwrap_or(Value::Null);
            let text = extract_text(&strip_system_instructions(&content));
            let mut title = python_strip(&text).replace('\n', " ");
            if title.is_empty() {
                title = tr("Image");
            }
            let chars: Vec<char> = title.chars().collect();
            if chars.len() > 50 {
                return format!("{}...", chars[..50].iter().collect::<String>());
            }
            return title;
        }
    }
    tr("New Conversation")
}

/// `str.strip()`.
pub fn python_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c))
}

/// What a conversation file holds.
pub struct Saved {
    pub id: String,
    pub history: Vec<Value>,
    pub created: String,
    pub agent: String,
    pub backend: Option<String>,
}

/// The original `_save_conversation` (callers skip a history of one message).
pub fn save(id: &str, history: &[Value], created: &str, backend: &str, agent: &str) {
    let mut data = Map::new();
    data.insert("id".into(), json!(id));
    data.insert("title".into(), json!(generate_title(history)));
    data.insert("created".into(), json!(created));
    data.insert("updated".into(), json!(now_iso()));
    data.insert("backend".into(), json!(backend));
    data.insert("agent".into(), json!(agent));
    data.insert("chat_history".into(), Value::Array(history.to_vec()));
    if let Err(error) = std::fs::write(file(id), pyjson::dumps(&Value::Object(data), Some(2))) {
        println!("Error saving conversation: {error}");
    }
}

/// The original `_load_conversation`'s reading part.
pub fn load(id: &str) -> Option<Saved> {
    let path = file(id);
    if !path.exists() {
        return None;
    }
    let data = std::fs::read_to_string(&path)
        .map_err(|error| error.to_string())
        .and_then(|text| crate::pyjson::loads(&text));
    let data = match data {
        Ok(Value::Object(data)) => data,
        Ok(_) => return None,
        Err(error) => {
            println!("Error loading conversation: {error}");
            return None;
        }
    };
    let id = data.get("id")?.as_str()?.to_owned();
    let history = data.get("chat_history")?.as_array()?.clone();
    Some(Saved {
        id,
        history,
        created: data
            .get("created")
            .map(config::python_str)
            .unwrap_or_default(),
        agent: data
            .get("agent")
            .map(config::python_str)
            .unwrap_or_else(|| DEFAULT_AGENT_NAME.into()),
        backend: data.get("backend").map(config::python_str),
    })
}

/// One row of the conversations list.
#[derive(Clone, Debug, PartialEq)]
pub struct Listed {
    pub id: String,
    pub title: String,
    pub updated: String,
    pub agent: String,
    pub backend: String,
}

/// The original `_list_conversations`: grouped by agent (built-in ones first
/// in their order, then the others alphabetically), newest first in a group.
pub fn list(backend_filter: Option<&str>) -> Vec<Listed> {
    let mut conversations = Vec::new();
    let Ok(entries) = std::fs::read_dir(dir()) else {
        return conversations;
    };
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.ends_with(".json") {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(entry.path()) else {
            continue;
        };
        let Ok(Value::Object(data)) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        let mut backend = data
            .get("backend")
            .map(config::python_str)
            .unwrap_or_else(|| "None".into());
        if backend == "qwen_cli" {
            backend = "direct".into();
        }
        if let Some(filter) = backend_filter.filter(|filter| !filter.is_empty())
            && backend != filter
        {
            continue;
        }
        let text_or =
            |key: &str, default: String| data.get(key).map(config::python_str).unwrap_or(default);
        conversations.push(Listed {
            id: text_or("id", name.replace(".json", "")),
            title: text_or("title", tr("Untitled")),
            updated: text_or("updated", String::new()),
            agent: text_or("agent", DEFAULT_AGENT_NAME.into()),
            backend,
        });
    }
    conversations.sort_by(|a, b| b.updated.cmp(&a.updated));
    conversations.sort_by_key(|conversation| {
        let agent = if conversation.agent.is_empty() {
            DEFAULT_AGENT_NAME
        } else {
            conversation.agent.as_str()
        };
        match config::BUILTIN_AGENT_NAMES
            .iter()
            .position(|name| *name == agent)
        {
            Some(index) => (0, index, String::new()),
            None => (1, 0, agent.to_lowercase()),
        }
    });
    conversations
}

/// The original `_delete_conversation`.
pub fn delete(id: &str) {
    let path = file(id);
    if path.exists()
        && let Err(error) = std::fs::remove_file(path)
    {
        println!("Error deleting conversation: {error}");
    }
}

/// The original `_rename_conversation`.
pub fn rename(id: &str, title: &str) {
    let path = file(id);
    let result = std::fs::read_to_string(&path)
        .map_err(|error| error.to_string())
        .and_then(|text| crate::pyjson::loads(&text))
        .and_then(|mut data| {
            let map = data
                .as_object_mut()
                .ok_or("list indices must be integers or slices, not str")?;
            map.insert("title".into(), json!(python_strip(title)));
            std::fs::write(&path, pyjson::dumps(&data, Some(2))).map_err(|error| error.to_string())
        });
    if let Err(error) = result {
        println!("Error renaming conversation: {error}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn content_helpers_match_the_original() {
        let content = json!([
            {"type": "text", "text": format!("{SCREEN_AWARENESS_PREFIX}what is this")},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}}
        ]);
        let stripped = strip_system_instructions(&content);
        assert_eq!(extract_text(&stripped), "what is this");
        assert_eq!(extract_images(&content), ["data:image/png;base64,AAA"]);
        let long = "x".repeat(60);
        let history = vec![
            json!({"role": "system", "content": "s"}),
            json!({"role": "user", "content": long}),
        ];
        assert_eq!(generate_title(&history), format!("{}...", "x".repeat(50)));
        assert_eq!(
            display_time("2026-10-06T00:26:22.339677"),
            "2026-10-06 00:26"
        );
        assert_eq!(display_time("yesterday"), "yesterday");
    }
}
