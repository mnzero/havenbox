use std::collections::{HashMap, HashSet};

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum JsonError {
    Syntax,
    DuplicateKey,
    MissingType,
}

pub(crate) fn top_level_strings(input: &[u8]) -> Result<HashMap<String, String>, JsonError> {
    let text = std::str::from_utf8(input).map_err(|_| JsonError::Syntax)?;
    let mut parser = Parser {
        chars: text.chars().collect(),
        index: 0,
    };
    parser.space();
    let values = parser.object(0, true)?;
    parser.space();
    if parser.index != parser.chars.len() {
        return Err(JsonError::Syntax);
    }
    if !values.contains_key("type") {
        return Err(JsonError::MissingType);
    }
    Ok(values)
}

struct Parser {
    chars: Vec<char>,
    index: usize,
}

impl Parser {
    fn space(&mut self) {
        while matches!(self.peek(), Some(' ' | '\n' | '\r' | '\t')) {
            self.index += 1;
        }
    }

    fn peek(&self) -> Option<char> {
        self.chars.get(self.index).copied()
    }

    fn take(&mut self, expected: char) -> Result<(), JsonError> {
        if self.peek() != Some(expected) {
            return Err(JsonError::Syntax);
        }
        self.index += 1;
        Ok(())
    }

    fn object(
        &mut self,
        depth: usize,
        capture: bool,
    ) -> Result<HashMap<String, String>, JsonError> {
        if depth >= 64 {
            return Err(JsonError::Syntax);
        }
        self.take('{')?;
        self.space();
        let mut keys = HashSet::new();
        let mut values = HashMap::new();
        if self.peek() == Some('}') {
            self.index += 1;
            return Ok(values);
        }
        loop {
            let key = self.string()?;
            if !keys.insert(key.clone()) {
                return Err(JsonError::DuplicateKey);
            }
            self.space();
            self.take(':')?;
            self.space();
            if capture && self.peek() == Some('"') {
                values.insert(key, self.string()?);
            } else {
                self.value(depth + 1)?;
            }
            self.space();
            match self.peek() {
                Some(',') => {
                    self.index += 1;
                    self.space();
                }
                Some('}') => {
                    self.index += 1;
                    break;
                }
                _ => return Err(JsonError::Syntax),
            }
        }
        Ok(values)
    }

    fn array(&mut self, depth: usize) -> Result<(), JsonError> {
        if depth >= 64 {
            return Err(JsonError::Syntax);
        }
        self.take('[')?;
        self.space();
        if self.peek() == Some(']') {
            self.index += 1;
            return Ok(());
        }
        loop {
            self.value(depth + 1)?;
            self.space();
            match self.peek() {
                Some(',') => {
                    self.index += 1;
                    self.space();
                }
                Some(']') => {
                    self.index += 1;
                    return Ok(());
                }
                _ => return Err(JsonError::Syntax),
            }
        }
    }

    fn value(&mut self, depth: usize) -> Result<(), JsonError> {
        self.space();
        match self.peek() {
            Some('{') => self.object(depth, false).map(|_| ()),
            Some('[') => self.array(depth),
            Some('"') => self.string().map(|_| ()),
            Some('t') => self.literal("true"),
            Some('f') => self.literal("false"),
            Some('n') => self.literal("null"),
            Some('-' | '0'..='9') => self.number(),
            _ => Err(JsonError::Syntax),
        }
    }

    fn literal(&mut self, value: &str) -> Result<(), JsonError> {
        for expected in value.chars() {
            self.take(expected)?;
        }
        Ok(())
    }

    fn number(&mut self) -> Result<(), JsonError> {
        if self.peek() == Some('-') {
            self.index += 1;
        }
        match self.peek() {
            Some('0') => self.index += 1,
            Some('1'..='9') => {
                self.index += 1;
                while matches!(self.peek(), Some('0'..='9')) {
                    self.index += 1;
                }
            }
            _ => return Err(JsonError::Syntax),
        }
        if self.peek() == Some('.') {
            self.index += 1;
            self.digits()?;
        }
        if matches!(self.peek(), Some('e' | 'E')) {
            self.index += 1;
            if matches!(self.peek(), Some('+' | '-')) {
                self.index += 1;
            }
            self.digits()?;
        }
        Ok(())
    }

    fn digits(&mut self) -> Result<(), JsonError> {
        let start = self.index;
        while matches!(self.peek(), Some('0'..='9')) {
            self.index += 1;
        }
        if self.index == start {
            Err(JsonError::Syntax)
        } else {
            Ok(())
        }
    }

    fn string(&mut self) -> Result<String, JsonError> {
        self.take('"')?;
        let mut result = String::new();
        loop {
            match self.peek().ok_or(JsonError::Syntax)? {
                '"' => {
                    self.index += 1;
                    return Ok(result);
                }
                '\\' => {
                    self.index += 1;
                    let escaped = self.peek().ok_or(JsonError::Syntax)?;
                    self.index += 1;
                    match escaped {
                        '"' | '\\' | '/' => result.push(escaped),
                        'b' => result.push('\u{0008}'),
                        'f' => result.push('\u{000c}'),
                        'n' => result.push('\n'),
                        'r' => result.push('\r'),
                        't' => result.push('\t'),
                        'u' => result.push(self.unicode_escape()?),
                        _ => return Err(JsonError::Syntax),
                    }
                }
                character if character < '\u{20}' => return Err(JsonError::Syntax),
                character => {
                    result.push(character);
                    self.index += 1;
                }
            }
        }
    }

    fn unicode_escape(&mut self) -> Result<char, JsonError> {
        let first = self.hex4()?;
        let scalar = if (0xd800..=0xdbff).contains(&first) {
            self.take('\\')?;
            self.take('u')?;
            let second = self.hex4()?;
            if !(0xdc00..=0xdfff).contains(&second) {
                return Err(JsonError::Syntax);
            }
            0x10000 + ((u32::from(first) - 0xd800) << 10) + (u32::from(second) - 0xdc00)
        } else if (0xdc00..=0xdfff).contains(&first) {
            return Err(JsonError::Syntax);
        } else {
            u32::from(first)
        };
        char::from_u32(scalar).ok_or(JsonError::Syntax)
    }

    fn hex4(&mut self) -> Result<u16, JsonError> {
        let mut value = 0_u16;
        for _ in 0..4 {
            let digit = self
                .peek()
                .and_then(|c| c.to_digit(16))
                .ok_or(JsonError::Syntax)?;
            self.index += 1;
            value = (value << 4) | digit as u16;
        }
        Ok(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_complete_json_and_duplicates() {
        let values =
            top_level_strings(br#"{"type":"Hello","instance":"a","nested":{"x":1}}"#).unwrap();
        assert_eq!(values["type"], "Hello");
        assert_eq!(
            top_level_strings(br#"{"type":"Hello","type":"Stop"}"#),
            Err(JsonError::DuplicateKey)
        );
        assert!(top_level_strings(br#"{"type":"Hello"}tail"#).is_err());
        assert!(top_level_strings(&[0xff]).is_err());
        let nested = format!(
            r#"{{"type":"Hello","value":{}}}"#,
            "[".repeat(65) + &"]".repeat(65)
        );
        assert!(top_level_strings(nested.as_bytes()).is_err());
    }
}
