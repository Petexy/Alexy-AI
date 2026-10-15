//! Alexy's themes: the original's `_discover_themes`, `_load_theme` (its CSS
//! part), `_scope_css` and `_get_theme_svg`.

use crate::config;
use crate::ui::tr;
use gtk::gdk;
use regex::Regex;
use serde_json::{Map, Value};
use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

/// One theme folder with its `theme.json`.
#[derive(Clone, Debug)]
pub struct Theme {
    pub id: String,
    pub path: PathBuf,
    pub name: String,
    pub author: String,
    pub description: String,
    pub css: Map<String, Value>,
}

/// The bundled themes: `themes/` beside the installed library (the original's
/// `/usr/share/linexin/widgets/themes/`).
pub fn bundled_dir() -> PathBuf {
    let beside = crate::widgets_dir().join("themes");
    if beside.is_dir() {
        beside
    } else {
        PathBuf::from(config::BUNDLED_THEMES_DIR)
    }
}

/// The original `_discover_themes`: bundled first, then the user's; a user
/// theme never replaces a bundled one of the same folder name.
pub fn discover() -> Vec<Theme> {
    let mut themes: Vec<Theme> = Vec::new();
    for root in [bundled_dir(), config::user_themes_dir()] {
        let Ok(entries) = std::fs::read_dir(&root) else {
            continue;
        };
        let mut names: Vec<String> = entries
            .flatten()
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        for entry in names {
            let path = root.join(&entry);
            let manifest = path.join("theme.json");
            if !manifest.is_file() {
                continue;
            }
            let data = std::fs::read_to_string(&manifest)
                .map_err(|error| error.to_string())
                .and_then(|text| crate::pyjson::loads(&text));
            let data = match data {
                Ok(Value::Object(data)) => data,
                Ok(_) => {
                    println!("Error reading theme {entry}: 'list' object has no attribute 'get'");
                    continue;
                }
                Err(error) => {
                    println!("Error reading theme {entry}: {error}");
                    continue;
                }
            };
            if themes.iter().any(|theme| theme.id == entry) {
                continue;
            }
            let text = |key: &str, default: String| {
                data.get(key).map(config::python_str).unwrap_or(default)
            };
            themes.push(Theme {
                name: text("name", entry.clone()),
                author: text("author", tr("Unknown")),
                description: text("description", String::new()),
                css: data
                    .get("css")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default(),
                id: entry,
                path,
            });
        }
    }
    themes
}

/// The widget's base rules, before a theme's.
const BASE_CSS: &str = "
        box.message-box { margin-top: 10px; margin-bottom: 10px; margin-left: 12px; margin-right: 12px; }
        .settings-sidebar { background-color: @window_bg_color; border-left: 1px solid @borders; padding-left: 12px; }
        .settings-sidebar-header { padding: 10px 14px; border-bottom: 1px solid @borders; }
        .settings-switcher-bar { padding: 8px 14px 0px 14px; }
        .settings-scrim { background-color: transparent; }
        .conv-sidebar { border-right: 1px solid @borders; margin-right: 6px; padding-right: 12px; }
        .conv-panel { background-color: @window_bg_color; border-right: 1px solid @borders; padding-right: 12px; padding-left: 4px; }
        ";

/// The chosen theme (`theme_id`, else the first), or `None` when there is none.
pub fn choose(theme_id: &str) -> Option<Theme> {
    let themes = discover();
    themes
        .iter()
        .find(|theme| theme.id == theme_id)
        .or(themes.first())
        .cloned()
}

/// The CSS of a theme: the base rules, the legacy `theme.json` colours and
/// the theme's own `style.css`.
pub fn css_for(theme: &Theme) -> String {
    let mut css = BASE_CSS.to_owned();
    let colour = |key: &str| {
        theme
            .css
            .get(key)
            .map(config::python_str)
            .filter(|value| !value.is_empty())
    };
    if let Some(value) = colour("assistant_bubble_bg") {
        css.push_str(&format!(
            "box.assistant-bubble {{ background-color: {value}; }}\n"
        ));
    }
    if let Some(value) = colour("user_bubble_bg") {
        css.push_str(&format!(
            "box.user-bubble {{ background-color: {value}; }}\n"
        ));
    }
    if let Some(value) = colour("accent_color") {
        css.push_str(&format!(
            "button.suggested-action {{ background-color: {value}; }}\n"
        ));
    }
    let style = theme.path.join("style.css");
    if style.is_file() {
        match std::fs::read_to_string(&style) {
            Ok(text) => {
                css.push('\n');
                css.push_str(&text);
            }
            Err(error) => println!("Error loading theme style.css: {error}"),
        }
    }
    css
}

thread_local! {
    /// The one display-wide provider every widget instance reloads (a second
    /// provider would keep an old theme's rules alive).
    static PROVIDER: RefCell<Option<gtk::CssProvider>> = const { RefCell::new(None) };
}

/// Apply a theme's CSS, scoped to the widget's root class.
pub fn apply(theme: &Theme) {
    let Some(display) = gdk::Display::default() else {
        return;
    };
    let scoped = scope_css(&css_for(theme), ".alexy-ai-root");
    PROVIDER.with(|provider| {
        let mut provider = provider.borrow_mut();
        let provider = provider.get_or_insert_with(|| {
            let provider = gtk::CssProvider::new();
            gtk::style_context_add_provider_for_display(
                &display,
                &provider,
                gtk::STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
            );
            provider
        });
        provider.load_from_string(&scoped);
    });
}

/// `_get_theme_svg`.
pub fn svg(theme_dir: Option<&Path>, name: &str) -> Option<PathBuf> {
    let path = theme_dir?.join(name);
    path.is_file().then_some(path)
}

/// The original `_scope_css`: every selector prefixed with `scope` (as a
/// descendant, and for a leading class/id/pseudo/attribute also on the root
/// itself); `@media`, `@supports` and `@container` bodies scoped too, other
/// at-rules left alone.
pub fn scope_css(css: &str, scope: &str) -> String {
    static COMMENTS: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?s)/\*.*?\*/").expect("valid"));
    let css = COMMENTS.replace_all(css, "");
    let bytes = css.as_bytes();
    let mut result: Vec<String> = Vec::new();
    let mut pos = 0;
    while pos < bytes.len() {
        let Some(brace) = css[pos..].find('{').map(|offset| pos + offset) else {
            let tail = css[pos..].trim();
            if !tail.is_empty() {
                result.push(tail.to_owned());
            }
            break;
        };
        let selector = &css[pos..brace];
        let mut depth = 1;
        let mut j = brace + 1;
        while j < bytes.len() && depth > 0 {
            match bytes[j] {
                b'{' => depth += 1,
                b'}' => depth -= 1,
                _ => {}
            }
            j += 1;
        }
        let body = &css[brace..j];
        let sel = selector.trim();
        if sel.starts_with('@') {
            let keyword = sel.split_whitespace().next().unwrap_or("").to_lowercase();
            if ["@media", "@supports", "@container"].contains(&keyword.as_str()) {
                let inner_body = body
                    .get(1..body.len().saturating_sub(1))
                    .unwrap_or_default();
                let inner = scope_css(inner_body, scope);
                result.push(format!("{sel} {{{inner}}}"));
            } else {
                result.push(format!("{sel} {body}"));
            }
        } else if !sel.is_empty() {
            let mut scoped = Vec::new();
            for part in sel
                .split(',')
                .map(str::trim)
                .filter(|part| !part.is_empty())
            {
                scoped.push(format!("{scope} {part}"));
                if part.starts_with(['.', '#', ':', '[']) {
                    scoped.push(format!("{scope}{part}"));
                }
            }
            result.push(format!("{} {body}", scoped.join(", ")));
        }
        pos = j;
    }
    result.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scoping_matches_the_python_original() {
        let css = "/* c */ list { a: b; }\n.dark box.x, row > y { c: d; }\n@media (prefers-color-scheme: dark) { .x { e: f; } }\n@define-color a #fff;\n@keyframes k { from { g: h; } }\n trailing";
        let script = r#"
import re, sys
def _scope_css(css_text, scope):
    css_text = re.sub(r"/\*.*?\*/", "", css_text, flags=re.DOTALL)
    result = []
    pos = 0
    length = len(css_text)
    while pos < length:
        brace = css_text.find("{", pos)
        if brace == -1:
            tail = css_text[pos:].strip()
            if tail:
                result.append(tail)
            break
        selector = css_text[pos:brace]
        depth = 1
        j = brace + 1
        while j < length and depth > 0:
            if css_text[j] == "{":
                depth += 1
            elif css_text[j] == "}":
                depth -= 1
            j += 1
        body = css_text[brace:j]
        sel = selector.strip()
        if sel.startswith("@"):
            keyword = sel.split()[0].lower() if sel else ""
            if keyword in ("@media", "@supports", "@container"):
                inner = _scope_css(body[1:-1], scope)
                result.append(f"{sel} {{{inner}}}")
            else:
                result.append(sel + " " + body)
        elif sel:
            parts = [p.strip() for p in sel.split(",") if p.strip()]
            scoped_parts = []
            for p in parts:
                variants = [f"{scope} {p}"]
                if p[:1] in ".#:[":
                    variants.append(f"{scope}{p}")
                scoped_parts.extend(variants)
            result.append(", ".join(scoped_parts) + " " + body)
        pos = j
    return "\n".join(result)
sys.stdout.write(_scope_css(sys.stdin.read(), ".alexy-ai-root"))
"#;
        let Ok(output) = std::process::Command::new("python3")
            .args(["-c", script])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .spawn()
            .and_then(|mut child| {
                use std::io::Write;
                child.stdin.take().unwrap().write_all(css.as_bytes())?;
                child.wait_with_output()
            })
        else {
            return;
        };
        assert_eq!(
            scope_css(css, ".alexy-ai-root"),
            String::from_utf8(output.stdout).unwrap()
        );
        for style in ["imessage", "matrix"] {
            let path = format!("/usr/share/linexin/widgets/themes/{style}/style.css");
            if let Ok(text) = std::fs::read_to_string(path) {
                assert!(!scope_css(&text, ".alexy-ai-root").is_empty());
            }
        }
    }
}
