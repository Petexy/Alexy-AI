//! The original `_markdown_to_pango`: a subset of Markdown as Pango markup
//! for the message labels.

use regex::{Captures, Regex};
use std::sync::LazyLock;

macro_rules! regex {
    ($pattern:expr) => {{
        static REGEX: LazyLock<Regex> = LazyLock::new(|| Regex::new($pattern).expect("valid"));
        &*REGEX
    }};
}

/// `html.escape(text)` (quotes too).
pub fn html_escape(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&#x27;"),
            c => escaped.push(c),
        }
    }
    escaped
}

pub fn to_pango(text: &str) -> String {
    let escaped = html_escape(text);
    // Triple backticks (with an optional language), then single ones.
    let markup = regex!(r"(?s)```[a-zA-Z0-9]*\n?(.*?)```").replace_all(&escaped, "<tt>${1}</tt>");
    let markup = regex!(r"(?s)`(.*?)`").replace_all(&markup, "<tt>${1}</tt>");

    // Keep <tt> blocks out of the bold/italic rules (underscores in names).
    let mut blocks: Vec<String> = Vec::new();
    let markup = regex!(r"(?s)<tt>.*?</tt>")
        .replace_all(&markup, |captures: &Captures| {
            blocks.push(captures[0].to_owned());
            format!("\x00TT{}\x00", blocks.len() - 1)
        })
        .into_owned();

    let markup = regex!(r"(?m)^### (.*?)$")
        .replace_all(&markup, r#"<span size="large" weight="bold">${1}</span>"#);
    let markup = regex!(r"(?m)^## (.*?)$")
        .replace_all(&markup, r#"<span size="x-large" weight="bold">${1}</span>"#);
    let markup = regex!(r"(?m)^# (.*?)$").replace_all(
        &markup,
        r#"<span size="xx-large" weight="bold">${1}</span>"#,
    );

    let markup = regex!(r"(?m)^[-*]\s+(.*?)$").replace_all(&markup, "  • ${1}");
    let markup = regex!(r"(?m)^(\d+)\.\s+(.*?)$").replace_all(&markup, "  ${1}. ${2}");

    let markup = regex!(r"(?s)\*\*(.*?)\*\*").replace_all(&markup, "<b>${1}</b>");
    // Italic after bold, so double asterisks are taken first.
    let markup = regex!(r"\*(.*?)\*").replace_all(&markup, "<i>${1}</i>");
    let mut markup = regex!(r"_(.*?)_")
        .replace_all(&markup, "<i>${1}</i>")
        .into_owned();

    for (index, block) in blocks.iter().enumerate() {
        markup = markup.replace(&format!("\x00TT{index}\x00"), block);
    }
    markup
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_the_python_original() {
        let samples = [
            "Hello **bold** and *it* _x_ & <tag>",
            "# H1\n## H2\n### H3\n- item\n* star\n1. one\n22. two",
            "```bash\nls my_file_name\n```\nand `a_b_c` **x**",
            "multi **line\nbold** `code\nspan`",
            "quotes \"double\" 'single'",
        ];
        let script = r#"
import html, re, sys, json
def md(text_content):
    escaped_content = html.escape(text_content)
    parsed_markup = re.sub(r'```[a-zA-Z0-9]*\n?(.*?)```', r'<tt>\1</tt>', escaped_content, flags=re.DOTALL)
    parsed_markup = re.sub(r'`(.*?)`', r'<tt>\1</tt>', parsed_markup, flags=re.DOTALL)
    _tt_blocks = []
    def _save_tt(m):
        _tt_blocks.append(m.group(0))
        return f'\x00TT{len(_tt_blocks)-1}\x00'
    parsed_markup = re.sub(r'<tt>.*?</tt>', _save_tt, parsed_markup, flags=re.DOTALL)
    parsed_markup = re.sub(r'^### (.*?)$', r'<span size="large" weight="bold">\1</span>', parsed_markup, flags=re.MULTILINE)
    parsed_markup = re.sub(r'^## (.*?)$', r'<span size="x-large" weight="bold">\1</span>', parsed_markup, flags=re.MULTILINE)
    parsed_markup = re.sub(r'^# (.*?)$', r'<span size="xx-large" weight="bold">\1</span>', parsed_markup, flags=re.MULTILINE)
    parsed_markup = re.sub(r'^[-*]\s+(.*?)$', r'  • \1', parsed_markup, flags=re.MULTILINE)
    parsed_markup = re.sub(r'^(\d+)\.\s+(.*?)$', r'  \1. \2', parsed_markup, flags=re.MULTILINE)
    parsed_markup = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', parsed_markup, flags=re.DOTALL)
    parsed_markup = re.sub(r'\*(.*?)\*', r'<i>\1</i>', parsed_markup)
    parsed_markup = re.sub(r'_(.*?)_', r'<i>\1</i>', parsed_markup)
    for i, block in enumerate(_tt_blocks):
        parsed_markup = parsed_markup.replace(f'\x00TT{i}\x00', block)
    return parsed_markup
print(json.dumps([md(s) for s in json.load(sys.stdin)]))
"#;
        let Ok(output) = std::process::Command::new("python3")
            .args(["-c", script])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .spawn()
            .and_then(|mut child| {
                use std::io::Write;
                child
                    .stdin
                    .take()
                    .unwrap()
                    .write_all(serde_json::to_string(&samples).unwrap().as_bytes())?;
                child.wait_with_output()
            })
        else {
            return;
        };
        let expected: Vec<String> = serde_json::from_slice(&output.stdout).unwrap();
        let ours: Vec<String> = samples.iter().map(|sample| to_pango(sample)).collect();
        assert_eq!(ours, expected);
    }
}
