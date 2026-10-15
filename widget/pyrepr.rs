//! `str()` and `repr()` of JSON values as Python shows them, for messages the
//! original built from parsed responses.

use serde_json::Value;

/// `str(value)`: strings as they are, everything else as `repr`.
pub fn str(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => repr(other),
    }
}

/// `repr(value)` of what `json.loads` returns.
pub fn repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(number) => {
            if number.is_f64()
                && let Some(float) = number.as_f64()
            {
                if float.is_finite() && float == float.trunc() && float.abs() < 1e16 {
                    return format!("{float:.1}");
                }
                return format!("{float}");
            }
            number.to_string()
        }
        Value::String(text) => repr_str(text),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(key, value)| format!("{}: {}", repr_str(key), repr(value)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

/// `repr()` of a `str`.
pub fn repr_str(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c if (0x80..0xa0).contains(&(c as u32)) => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn reprs_like_python() {
        assert_eq!(
            repr(&json!({"error": {"message": "bad", "code": 1}, "x": [true, null, 1.5, "it's"]})),
            r#"{'error': {'message': 'bad', 'code': 1}, 'x': [True, None, 1.5, "it's"]}"#
        );
        assert_eq!(str(&json!("plain")), "plain");
    }
}
