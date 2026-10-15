//! `json.dump`/`json.dumps` as Python writes them (the original's config and
//! conversation files), so the files stay byte for byte what they were.

use serde_json::Value;

/// `json.dumps(value, indent=indent)`: with no indent, `, ` and `: `
/// separators on one line; with one, `,` at line ends; non-ASCII escaped.
pub fn dumps(value: &Value, indent: Option<usize>) -> String {
    let mut out = String::new();
    write(&mut out, value, indent, 0);
    out
}

fn write(out: &mut String, value: &Value, indent: Option<usize>, level: usize) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => out.push_str(&python_number(number)),
        Value::String(text) => string(out, text),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                separate(out, indent, level + 1, index == 0);
                write(out, item, indent, level + 1);
            }
            close(out, indent, level);
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (index, (key, item)) in map.iter().enumerate() {
                separate(out, indent, level + 1, index == 0);
                string(out, key);
                out.push_str(": ");
                write(out, item, indent, level + 1);
            }
            close(out, indent, level);
            out.push('}');
        }
    }
}

fn separate(out: &mut String, indent: Option<usize>, level: usize, first: bool) {
    match indent {
        Some(width) => {
            if !first {
                out.push(',');
            }
            out.push('\n');
            out.extend(std::iter::repeat_n(' ', width * level));
        }
        None => {
            if !first {
                out.push_str(", ");
            }
        }
    }
}

fn close(out: &mut String, indent: Option<usize>, level: usize) {
    if let Some(width) = indent {
        out.push('\n');
        out.extend(std::iter::repeat_n(' ', width * level));
    }
}

fn python_number(number: &serde_json::Number) -> String {
    if let Some(float) = number.as_f64().filter(|_| number.is_f64()) {
        if float.is_finite() && float == float.trunc() && float.abs() < 1e16 {
            return format!("{float:.1}");
        }
        return format!("{float}");
    }
    number.to_string()
}

/// A JSON string with `ensure_ascii`.
fn string(out: &mut String, text: &str) {
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `json.loads(text)`: the value, or the message Python's `JSONDecodeError`
/// would have ("Expecting value: line 1 column 1 (char 0)", ...).
pub fn loads(text: &str) -> Result<Value, String> {
    serde_json::from_str(text)
        .map_err(|error| decode_error(text).unwrap_or_else(|| error.to_string()))
}

/// Where and why Python's C scanner rejects `text` (None if it would not).
fn decode_error(text: &str) -> Option<String> {
    let chars: Vec<char> = text.chars().collect();
    if chars.first() == Some(&'\u{feff}') {
        return Some(located(
            &chars,
            "Unexpected UTF-8 BOM (decode using utf-8-sig)",
            0,
        ));
    }
    let start = skip_whitespace(&chars, 0);
    let result = scan(&chars, start).and_then(|end| {
        let end = skip_whitespace(&chars, end);
        if end == chars.len() {
            Ok(end)
        } else {
            Err(("Extra data", end))
        }
    });
    result
        .err()
        .map(|(message, at)| located(&chars, message, at))
}

fn located(chars: &[char], message: &str, at: usize) -> String {
    let line = chars[..at].iter().filter(|c| **c == '\n').count() + 1;
    let column = match chars[..at].iter().rposition(|c| *c == '\n') {
        Some(newline) => at - newline,
        None => at + 1,
    };
    format!("{message}: line {line} column {column} (char {at})")
}

fn skip_whitespace(chars: &[char], mut at: usize) -> usize {
    while at < chars.len() && matches!(chars[at], ' ' | '\t' | '\n' | '\r') {
        at += 1;
    }
    at
}

type Scan = Result<usize, (&'static str, usize)>;

/// `scan_once`: the index after the value at `at`.
fn scan(chars: &[char], at: usize) -> Scan {
    let Some(&first) = chars.get(at) else {
        return Err(("Expecting value", at));
    };
    let word = |word: &str| {
        let length = word.chars().count();
        chars.len() >= at + length && chars[at..at + length].iter().copied().eq(word.chars())
    };
    match first {
        '"' => return scan_string(chars, at + 1),
        '{' => return scan_object(chars, at + 1),
        '[' => return scan_array(chars, at + 1),
        'n' if word("null") => return Ok(at + 4),
        't' if word("true") => return Ok(at + 4),
        'f' if word("false") => return Ok(at + 5),
        'N' if word("NaN") => return Ok(at + 3),
        'I' if word("Infinity") => return Ok(at + 8),
        '-' if word("-Infinity") => return Ok(at + 9),
        _ => {}
    }
    // A number.
    let digit = |index: usize| chars.get(index).is_some_and(char::is_ascii_digit);
    let mut index = at;
    if chars[index] == '-' {
        index += 1;
    }
    match chars.get(index) {
        Some('1'..='9') => {
            while digit(index) {
                index += 1;
            }
        }
        Some('0') => index += 1,
        _ => return Err(("Expecting value", at)),
    }
    if chars.get(index) == Some(&'.') && digit(index + 1) {
        index += 1;
        while digit(index) {
            index += 1;
        }
    }
    if matches!(chars.get(index), Some('e' | 'E')) {
        let mut exponent = index + 1;
        if matches!(chars.get(exponent), Some('-' | '+')) {
            exponent += 1;
        }
        if digit(exponent) {
            while digit(exponent) {
                exponent += 1;
            }
            index = exponent;
        }
    }
    Ok(index)
}

/// `scanstring`: `at` is just after the opening quote.
fn scan_string(chars: &[char], at: usize) -> Scan {
    let begin = at - 1;
    let mut index = at;
    loop {
        let found = (index..chars.len())
            .find(|&next| matches!(chars[next], '"' | '\\') || chars[next] <= '\u{1f}');
        let Some(next) = found else {
            return Err(("Unterminated string starting at", begin));
        };
        match chars[next] {
            '"' => return Ok(next + 1),
            '\\' => {}
            _ => return Err(("Invalid control character at", next)),
        }
        let Some(&escape) = chars.get(next + 1) else {
            return Err(("Unterminated string starting at", begin));
        };
        if escape != 'u' {
            if !matches!(escape, '"' | '\\' | '/' | 'b' | 'f' | 'n' | 'r' | 't') {
                return Err(("Invalid \\escape", next));
            }
            index = next + 2;
            continue;
        }
        let hex_at = next + 2;
        let end = hex_at + 4;
        if end >= chars.len() {
            return Err(("Invalid \\uXXXX escape", hex_at - 1));
        }
        if !chars[hex_at..end].iter().all(char::is_ascii_hexdigit) {
            return Err(("Invalid \\uXXXX escape", end - 5));
        }
        index = end;
        // A high surrogate followed by \uXXXX: that escape must be well formed too.
        let value =
            u32::from_str_radix(&chars[hex_at..end].iter().collect::<String>(), 16).unwrap_or(0);
        if (0xd800..=0xdbff).contains(&value)
            && chars.get(end) == Some(&'\\')
            && chars.get(end + 1) == Some(&'u')
        {
            let low_end = end + 6;
            if low_end >= chars.len() {
                return Err(("Invalid \\uXXXX escape", end + 1));
            }
            if !chars[end + 2..low_end].iter().all(char::is_ascii_hexdigit) {
                return Err(("Invalid \\uXXXX escape", low_end - 5));
            }
            let low = u32::from_str_radix(&chars[end + 2..low_end].iter().collect::<String>(), 16)
                .unwrap_or(0);
            if (0xdc00..=0xdfff).contains(&low) {
                index = low_end;
            }
        }
    }
}

fn scan_object(chars: &[char], at: usize) -> Scan {
    let mut index = skip_whitespace(chars, at);
    if chars.get(index) == Some(&'}') {
        return Ok(index + 1);
    }
    loop {
        if chars.get(index) != Some(&'"') {
            return Err(("Expecting property name enclosed in double quotes", index));
        }
        index = skip_whitespace(chars, scan_string(chars, index + 1)?);
        if chars.get(index) != Some(&':') {
            return Err(("Expecting ':' delimiter", index));
        }
        index = skip_whitespace(chars, index + 1);
        index = skip_whitespace(chars, scan(chars, index)?);
        match chars.get(index) {
            Some('}') => return Ok(index + 1),
            Some(',') => {}
            _ => return Err(("Expecting ',' delimiter", index)),
        }
        let comma = index;
        index = skip_whitespace(chars, index + 1);
        if chars.get(index) == Some(&'}') {
            return Err(("Illegal trailing comma before end of object", comma));
        }
    }
}

fn scan_array(chars: &[char], at: usize) -> Scan {
    let mut index = skip_whitespace(chars, at);
    if chars.get(index) == Some(&']') {
        return Ok(index + 1);
    }
    loop {
        index = skip_whitespace(chars, scan(chars, index)?);
        match chars.get(index) {
            Some(']') => return Ok(index + 1),
            Some(',') => {}
            _ => return Err(("Expecting ',' delimiter", index)),
        }
        let comma = index;
        index = skip_whitespace(chars, index + 1);
        if chars.get(index) == Some(&']') {
            return Err(("Illegal trailing comma before end of array", comma));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn dumps_like_python() {
        let value = json!({"a": [1, 2.5, true, null], "b": {}, "c": [], "d": {"e": "x"}});
        assert_eq!(
            dumps(&value, None),
            r#"{"a": [1, 2.5, true, null], "b": {}, "c": [], "d": {"e": "x"}}"#
        );
        assert_eq!(
            dumps(&value, Some(2)),
            "{\n  \"a\": [\n    1,\n    2.5,\n    true,\n    null\n  ],\n  \"b\": {},\n  \"c\": [],\n  \"d\": {\n    \"e\": \"x\"\n  }\n}"
        );
        assert_eq!(dumps(&json!(1.0), None), "1.0");
        let polish: String = ['\u{141}', 'a', '\u{1f600}', '\u{7f}'].iter().collect();
        let slash = '\\';
        assert_eq!(
            dumps(&json!(polish), None),
            format!("\"{slash}u0141a{slash}ud83d{slash}ude00{slash}u007f\"")
        );
    }

    #[test]
    fn matches_python_itself() {
        let value = json!({"x": ["\u{e9}", "q\"t", "\n", 3, -1.25, {"y": []}]});
        let Ok(output) = std::process::Command::new("python3")
            .args([
                "-c",
                "import json,sys; v=json.load(sys.stdin); print(json.dumps(v, indent=4)); print(json.dumps(v))",
            ])
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .spawn()
            .and_then(|mut child| {
                use std::io::Write;
                child
                    .stdin
                    .take()
                    .unwrap()
                    .write_all(value.to_string().as_bytes())?;
                child.wait_with_output()
            })
        else {
            return;
        };
        let expected = String::from_utf8(output.stdout).unwrap();
        assert_eq!(
            expected,
            format!("{}\n{}\n", dumps(&value, Some(4)), dumps(&value, None))
        );
    }
    #[test]
    fn decode_errors_match_python() {
        let cases = [
            "",
            " ",
            "{\"backend\": ",
            "{",
            "[",
            "{\"a\" 1}",
            "{\"a\": 1 \"b\": 2}",
            "{\"a\": 1,}",
            "[1, 2,]",
            "[1 2]",
            "{'a': 1}",
            "{\"a\": \"x\ny\"}",
            "{\"a\": \"\\q\"}",
            "\"\\u12\"",
            "\"\\u12zz\"",
            "\"abc",
            "{\"a\": 1} x",
            "1.",
            "-",
            "nul",
            "{\"a\": tru}",
            "\n\n  [1,\n  ]",
            "\u{feff}{}",
            "{\"a\": \"\\ud83d\\u12\"}",
            "[1e5, 2E-3, -0.5, 1e]",
            "\"\\",
            "[\"a\\",
        ];
        let script = "import json, sys\n\
            for case in json.load(sys.stdin):\n\
            \x20   try:\n\
            \x20       json.loads(case)\n\
            \x20       print('OK')\n\
            \x20   except json.JSONDecodeError as e:\n\
            \x20       print(e)\n";
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
                    .write_all(serde_json::to_string(&cases).unwrap().as_bytes())?;
                child.wait_with_output()
            })
        else {
            return;
        };
        let expected = String::from_utf8(output.stdout).unwrap();
        assert_eq!(expected.lines().count(), cases.len());
        for (case, expected) in cases.iter().zip(expected.lines()) {
            let got = decode_error(case).unwrap_or_else(|| "OK".into());
            assert_eq!(got, expected, "{case:?}");
        }
    }
}
