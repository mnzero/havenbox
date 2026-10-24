//! Complete hb.channel.v1 framing and admission checks.
mod channel;
mod json;
mod sha256;

pub use channel::{
    Channel, ChannelQueue, Direction, Frame, FrameError, FrameReader, MessageKind, RequestRegistry,
};

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

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Header {
    pub channel: Channel,
    pub epoch: u64,
    pub request: [u8; 16],
    pub payload_length: u32,
    pub payload_sha256: [u8; 32],
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
        let channel = Channel::try_from(bytes[6]).map_err(|_| HeaderError::Channel)?;
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
        if u64::from(length) > channel.payload_limit() {
            return Err(HeaderError::PayloadLimit);
        }
        let request = bytes[16..32].try_into().expect("fixed header slice");
        let payload_sha256 = bytes[36..68].try_into().expect("fixed header slice");
        Ok(Self {
            channel,
            epoch,
            request,
            payload_length: length,
            payload_sha256,
        })
    }

    pub fn encode(&self) -> [u8; HEADER_BYTES] {
        let mut bytes = [0; HEADER_BYTES];
        bytes[..4].copy_from_slice(b"HBX1");
        bytes[4..6].copy_from_slice(&VERSION.to_be_bytes());
        bytes[6] = self.channel as u8;
        bytes[8..16].copy_from_slice(&self.epoch.to_be_bytes());
        bytes[16..32].copy_from_slice(&self.request);
        bytes[32..36].copy_from_slice(&self.payload_length.to_be_bytes());
        bytes[36..68].copy_from_slice(&self.payload_sha256);
        bytes
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
