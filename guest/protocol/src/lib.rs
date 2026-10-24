//! Header preflight only: not a transport, payload decoder or authorization boundary.
pub const VERSION: u16 = 1;
pub const HEADER_BYTES: usize = 68;

#[derive(Debug, PartialEq, Eq)]
pub enum HeaderError {
    Size,
    Magic,
    Version,
    Channel,
    Flags,
    Epoch,
    PayloadLimit,
}

#[derive(Debug, PartialEq, Eq)]
pub struct Header {
    pub channel: u8,
    pub epoch: u64,
    pub payload_length: u32,
}

impl Header {
    pub fn parse(bytes: &[u8], expected_epoch: u64) -> Result<Self, HeaderError> {
        if bytes.len() != HEADER_BYTES {
            return Err(HeaderError::Size);
        }
        if &bytes[..4] != b"HBX1" {
            return Err(HeaderError::Magic);
        }
        if u16::from_be_bytes([bytes[4], bytes[5]]) != VERSION {
            return Err(HeaderError::Version);
        }
        let limit = match bytes[6] {
            1 => 65_536,
            2 => 262_144,
            3 => 4_194_304,
            _ => return Err(HeaderError::Channel),
        };
        if bytes[7] != 0 {
            return Err(HeaderError::Flags);
        }
        let epoch = bytes[8..16]
            .iter()
            .fold(0_u64, |n, b| (n << 8) | u64::from(*b));
        if epoch != expected_epoch {
            return Err(HeaderError::Epoch);
        }
        let length = u32::from_be_bytes([bytes[32], bytes[33], bytes[34], bytes[35]]);
        if length > limit {
            return Err(HeaderError::PayloadLimit);
        }
        Ok(Self {
            channel: bytes[6],
            epoch,
            payload_length: length,
        })
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct InvalidWireId;
macro_rules! wire_id {
    ($name:ident, $prefix:literal) => {
        #[derive(Debug, Clone, PartialEq, Eq, Hash)]
        pub struct $name(String);
        impl $name {
            pub fn parse(value: &str) -> Result<Self, InvalidWireId> {
                let suffix = value.strip_prefix($prefix).ok_or(InvalidWireId)?;
                if suffix.is_empty()
                    || !suffix
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'-')
                {
                    return Err(InvalidWireId);
                }
                Ok(Self(value.to_owned()))
            }
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }
    };
}
wire_id!(SessionId, "session_");
wire_id!(TurnId, "turn_");
wire_id!(TaskId, "task_");
wire_id!(CallId, "call_");
wire_id!(FileVersionId, "file_");

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shared_header_vectors() {
        for line in include_str!("../../../contracts/header-vectors.tsv")
            .lines()
            .filter(|l| !l.starts_with('#'))
        {
            let fields: Vec<_> = line.split('\t').collect();
            let bytes: Vec<u8> = fields[1]
                .as_bytes()
                .chunks_exact(2)
                .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
                .collect();
            let result = Header::parse(&bytes, 7);
            let actual = match result {
                Ok(_) => "ok".into(),
                Err(e) => format!("{e:?}").to_lowercase(),
            };
            assert_eq!(actual, fields[2], "{}", fields[0]);
        }
    }
    #[test]
    fn typed_ids_reject_cross_kind_and_invalid_suffix() {
        assert!(SessionId::parse("session_A-1").is_ok());
        for bad in ["turn_A", "session_", "session_é", "session_a/b"] {
            assert!(SessionId::parse(bad).is_err());
        }
        assert!(TurnId::parse("turn_1").is_ok());
        assert!(TaskId::parse("task_1").is_ok());
        assert!(CallId::parse("call_1").is_ok());
        assert!(FileVersionId::parse("file_1").is_ok());
    }
}
