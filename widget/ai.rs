//! Talking to the models: the original's `call_direct_api`,
//! `call_endpoint_api`, `call_local_ollama`, `_execute_urllib_request` (the
//! HTTP part), `_endpoint_base_url`, `_discover_endpoint_model` and
//! `_correct_voice_text`.

use crate::pyrepr;
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Read};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

/// Where a chat request goes.
#[derive(Clone, Debug)]
pub enum Target {
    /// An online OpenAI-compatible API, with its key.
    Direct {
        url: String,
        key: String,
        model: String,
    },
    /// A local OpenAI-compatible server (no key).
    Endpoint { url: String, model: String },
    /// Ollama's own API.
    Ollama { url: String, model: String },
}

impl Target {
    pub fn is_ollama(&self) -> bool {
        matches!(self, Target::Ollama { .. })
    }
}

/// How a request ended.
#[derive(Debug)]
pub enum Outcome {
    /// The whole reply (inline `<think>` blocks included) and reasoning.
    Done { content: String },
    /// Stopped by the user while reading.
    Aborted,
    /// The server answered with an error status.
    Http {
        code: u16,
        reason: String,
        body: String,
    },
    /// The connection could not be made (`URLError`).
    Connection(String),
    /// An error object inside an Ollama stream.
    OllamaError(String),
    /// A non-streamed answer without a message.
    Unexpected(String),
    /// Anything else (`Exception`).
    Other(String),
}

/// `_endpoint_base_url`: the OpenAI-compatible base ending in `/v1`.
pub fn endpoint_base_url(url: &str) -> String {
    let mut base = url.trim().trim_end_matches('/').to_owned();
    if let Some(stripped) = base.strip_suffix("/chat/completions") {
        base = stripped.to_owned();
    }
    let base = base.trim_end_matches('/').to_owned();
    if !base.is_empty() && !(base.ends_with("/v1") || base.contains("/v1/")) {
        return format!("{base}/v1");
    }
    base
}

/// The direct API's URL: `/chat/completions` appended when missing.
pub fn direct_url(api_url: &str) -> String {
    let url = api_url.trim_end_matches('/');
    if url.ends_with("/chat/completions") {
        url.to_owned()
    } else {
        format!("{url}/chat/completions")
    }
}

fn agent(timeout: u64) -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_secs(timeout))
        .timeout_read(Duration::from_secs(timeout))
        .timeout_write(Duration::from_secs(timeout))
        .build()
}

/// `str(URLError)` for a transport failure.
fn urlopen_error(error: &ureq::Transport) -> String {
    use std::error::Error;
    let mut source = error.source();
    while let Some(current) = source {
        if let Some(io) = current.downcast_ref::<std::io::Error>()
            && let Some(code) = io.raw_os_error()
        {
            // SAFETY: strerror returns a NUL-terminated message for any number.
            let message =
                unsafe { std::ffi::CStr::from_ptr(libc::strerror(code)) }.to_string_lossy();
            return format!("<urlopen error [Errno {code}] {message}>");
        }
        source = current.source();
    }
    format!(
        "<urlopen error {}>",
        error
            .message()
            .map_or_else(|| error.to_string(), str::to_owned)
    )
}

/// `_discover_endpoint_model`: the first model the server lists.
pub fn discover_endpoint_model(endpoint_url: &str) -> Option<String> {
    let models_url = format!("{}/models", endpoint_base_url(endpoint_url));
    let result = agent(15)
        .get(&models_url)
        .set("Content-Type", "application/json")
        .call()
        .map_err(|error| match error {
            ureq::Error::Status(code, response) => {
                format!("HTTP Error {code}: {}", response.status_text())
            }
            ureq::Error::Transport(transport) => urlopen_error(&transport),
        })
        .and_then(|response| response.into_string().map_err(|error| error.to_string()))
        .and_then(|text| crate::pyjson::loads(&text));
    match result {
        Ok(payload) => {
            let first = payload
                .as_object()
                .and_then(|payload| payload.get("data"))
                .and_then(Value::as_array)
                .and_then(|items| items.first())
                .and_then(Value::as_object)?;
            let model = first
                .get("id")
                .filter(|id| crate::config::truthy(id))
                .or_else(|| first.get("name"))
                .filter(|id| crate::config::truthy(id))
                .map(pyrepr::str)?;
            println!("[Endpoint] Auto-detected model: {model}");
            Some(model)
        }
        Err(error) => {
            println!("[Endpoint] Model auto-detection failed: {error}");
            None
        }
    }
}

/// `call_local_ollama`'s message conversion: images as Ollama's `images`.
pub fn ollama_messages(history: &[Value]) -> Vec<Value> {
    history
        .iter()
        .map(|message| match message.get("content") {
            Some(Value::Array(items)) => {
                let mut text_parts = Vec::new();
                let mut images = Vec::new();
                for item in items {
                    match item.get("type").and_then(Value::as_str) {
                        Some("text") => {
                            text_parts.push(item.get("text").map(pyrepr::str).unwrap_or_default())
                        }
                        Some("image_url") => {
                            if let Some(url) = item
                                .get("image_url")
                                .and_then(|url| url.get("url"))
                                .and_then(Value::as_str)
                                && let Some((_, data)) = url.split_once(',')
                            {
                                images.push(json!(data));
                            }
                        }
                        _ => {}
                    }
                }
                let mut converted = serde_json::Map::new();
                converted.insert(
                    "role".into(),
                    message.get("role").cloned().unwrap_or(Value::Null),
                );
                converted.insert("content".into(), json!(text_parts.join("\n")));
                if !images.is_empty() {
                    converted.insert("images".into(), Value::Array(images));
                }
                Value::Object(converted)
            }
            _ => message.clone(),
        })
        .collect()
}

/// The request's URL, body and headers.
fn prepare(target: &Target, history: &[Value]) -> (String, Value, Vec<(&'static str, String)>) {
    match target {
        Target::Direct { url, key, model } => (
            direct_url(url),
            json!({"model": model, "messages": history, "stream": true}),
            vec![
                ("Content-Type", "application/json".into()),
                ("Authorization", format!("Bearer {key}")),
            ],
        ),
        Target::Endpoint { url, model } => (
            format!("{}/chat/completions", endpoint_base_url(url)),
            json!({"model": if model.is_empty() { "local-model" } else { model }, "messages": history, "stream": true}),
            vec![("Content-Type", "application/json".into())],
        ),
        Target::Ollama { url, model } => (
            url.clone(),
            json!({"model": model, "messages": ollama_messages(history), "stream": true}),
            vec![("Content-Type", "application/json".into())],
        ),
    }
}

/// The original `_execute_urllib_request`'s HTTP part: the request sent,
/// `begin` called once the server answered, `push` for every piece of
/// content and reasoning. Blocking.
pub fn chat(
    target: &Target,
    history: &[Value],
    abort: &AtomicBool,
    begin: &dyn Fn(),
    push: &dyn Fn(&str, &str),
) -> Outcome {
    let (url, body, headers) = prepare(target, history);
    let mut request = agent(600).post(&url);
    for (key, value) in &headers {
        request = request.set(key, value);
    }
    let response = match request.send_bytes(body.to_string().as_bytes()) {
        Ok(response) => response,
        Err(ureq::Error::Status(code, response)) => {
            let reason = response.status_text().to_owned();
            let body = response.into_string().unwrap_or_default();
            return Outcome::Http { code, reason, body };
        }
        Err(ureq::Error::Transport(transport)) => {
            return Outcome::Connection(urlopen_error(&transport));
        }
    };
    let is_sse = response
        .header("Content-Type")
        .unwrap_or_default()
        .to_lowercase()
        .contains("text/event-stream");
    begin();
    let mut content = String::new();
    let mut reader = BufReader::new(response.into_reader());
    if is_sse {
        for raw in split_lines(&mut reader) {
            if abort.load(Ordering::SeqCst) {
                break;
            }
            let line = String::from_utf8_lossy(&raw);
            let line = line.trim();
            let Some(payload) = line.strip_prefix("data:") else {
                continue;
            };
            let payload = payload.trim();
            if payload == "[DONE]" {
                break;
            }
            let Ok(object) = serde_json::from_str::<Value>(payload) else {
                continue;
            };
            let Some(choice) = object
                .get("choices")
                .and_then(Value::as_array)
                .and_then(|choices| choices.first())
            else {
                continue;
            };
            let delta = choice.get("delta").cloned().unwrap_or(Value::Null);
            let c = text_of(delta.get("content"));
            let r = {
                let reasoning = text_of(delta.get("reasoning_content"));
                if reasoning.is_empty() {
                    text_of(delta.get("reasoning"))
                } else {
                    reasoning
                }
            };
            if !c.is_empty() || !r.is_empty() {
                content.push_str(&c);
                push(&c, &r);
            }
        }
    } else if target.is_ollama() {
        for raw in split_lines(&mut reader) {
            if abort.load(Ordering::SeqCst) {
                break;
            }
            let line = String::from_utf8_lossy(&raw);
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let Ok(object) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            if let Some(error) = object
                .get("error")
                .filter(|error| crate::config::truthy(error))
            {
                return Outcome::OllamaError(pyrepr::str(error));
            }
            let message = object.get("message").cloned().unwrap_or(Value::Null);
            let c = text_of(message.get("content"));
            let r = text_of(message.get("thinking"));
            if !c.is_empty() || !r.is_empty() {
                content.push_str(&c);
                push(&c, &r);
            }
            if object.get("done").is_some_and(crate::config::truthy) {
                break;
            }
        }
    } else {
        let mut text = String::new();
        if let Err(error) = reader.read_to_string(&mut text) {
            return Outcome::Other(format!("Error: {error}"));
        }
        let result: Value = match crate::pyjson::loads(&text) {
            Ok(result) => result,
            Err(error) => return Outcome::Other(format!("Error: {error}")),
        };
        let message = result
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|choices| choices.first())
            .and_then(|choice| choice.get("message"));
        let Some(message) = message else {
            let mut error = result.as_object().and_then(|map| map.get("error")).cloned();
            if let Some(Value::Object(map)) = &error {
                error = Some(
                    map.get("message")
                        .cloned()
                        .unwrap_or(Value::Object(map.clone())),
                );
            }
            let shown = match error.filter(crate::config::truthy) {
                Some(error) => pyrepr::str(&error),
                None => pyrepr::str(&result),
            };
            return Outcome::Unexpected(shown);
        };
        content = text_of(message.get("content"));
        let reasoning = {
            let reasoning = text_of(message.get("reasoning_content"));
            if reasoning.is_empty() {
                text_of(message.get("reasoning"))
            } else {
                reasoning
            }
        };
        if !content.is_empty() || !reasoning.is_empty() {
            push(&content, &reasoning);
        }
    }
    if abort.load(Ordering::SeqCst) {
        return Outcome::Aborted;
    }
    Outcome::Done { content }
}

/// `value or ''` for a text field.
fn text_of(value: Option<&Value>) -> String {
    match value {
        Some(value) if crate::config::truthy(value) => pyrepr::str(value),
        _ => String::new(),
    }
}

/// Lines as iterating a response gives them (ending at `\n`).
fn split_lines(reader: &mut impl BufRead) -> impl Iterator<Item = Vec<u8>> + '_ {
    std::iter::from_fn(move || {
        let mut line = Vec::new();
        match reader.read_until(b'\n', &mut line) {
            Ok(0) | Err(_) => None,
            Ok(_) => Some(line),
        }
    })
}

/// The original `_correct_voice_text`: a one-shot request (direct API only)
/// that fixes a transcription; the original text on any failure.
pub fn correct_voice_text(raw_text: &str, url: &str, key: &str, model: &str) -> String {
    let prompt = concat!(
        "You are a text correction assistant. The following text was produced by ",
        "speech-to-text and may contain errors. Fix punctuation, capitalization, ",
        "and obviously misheard words. If the text has no sense and you feel the meaning was different by the context of the message, you can change it.",
        "Return ONLY the corrected text, nothing else. ",
        "Do not add explanations."
    );
    let body = json!({
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": raw_text}
        ]
    });
    let result = agent(60)
        .post(&direct_url(url))
        .set("Content-Type", "application/json")
        .set("Authorization", &format!("Bearer {key}"))
        .send_bytes(body.to_string().as_bytes())
        .map_err(|error| match error {
            ureq::Error::Status(code, response) => {
                format!("HTTP Error {code}: {}", response.status_text())
            }
            ureq::Error::Transport(transport) => urlopen_error(&transport),
        })
        .and_then(|response| response.into_string().map_err(|error| error.to_string()))
        .and_then(|text| crate::pyjson::loads(&text))
        .and_then(|result| {
            result
                .pointer("/choices/0/message/content")
                .and_then(Value::as_str)
                .map(|text| text.trim().to_owned())
                .ok_or_else(|| "'choices'".to_owned())
        });
    match result {
        Ok(corrected) if !corrected.is_empty() => corrected,
        Ok(_) => raw_text.to_owned(),
        Err(error) => {
            println!("[Voice correction] Failed, using raw text: {error}");
            raw_text.to_owned()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn urls_are_normalized_like_the_original() {
        assert_eq!(
            endpoint_base_url("http://127.0.0.1:1234"),
            "http://127.0.0.1:1234/v1"
        );
        assert_eq!(endpoint_base_url("http://h/v1/"), "http://h/v1");
        assert_eq!(
            endpoint_base_url("http://h/v1/chat/completions"),
            "http://h/v1"
        );
        assert_eq!(endpoint_base_url(""), "");
        assert_eq!(direct_url("https://x/v1/"), "https://x/v1/chat/completions");
        assert_eq!(
            direct_url("https://x/v1/chat/completions"),
            "https://x/v1/chat/completions"
        );
    }

    #[test]
    fn ollama_gets_its_image_field() {
        let history = vec![json!({"role": "user", "content": [
            {"type": "text", "text": "a"}, {"type": "text", "text": "b"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}}
        ]})];
        assert_eq!(
            ollama_messages(&history),
            vec![json!({"role": "user", "content": "a\nb", "images": ["QUJD"]})]
        );
    }
}
