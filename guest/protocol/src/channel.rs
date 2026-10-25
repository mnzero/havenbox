use crate::{HEADER_BYTES, Header, HeaderError, json, sha256};
use std::collections::{HashSet, VecDeque};
use std::io::Read;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
#[repr(u8)]
pub enum Channel {
    Control = 1,
    File = 2,
    Inference = 3,
}

impl Channel {
    pub const fn payload_limit(self) -> u64 {
        match self {
            Self::Control => 65_536,
            Self::File => 262_144,
            Self::Inference => 4_194_304,
        }
    }
}

impl TryFrom<u8> for Channel {
    type Error = ();
    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            1 => Ok(Self::Control),
            2 => Ok(Self::File),
            3 => Ok(Self::Inference),
            _ => Err(()),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    HostToGuest,
    GuestToHost,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MessageKind {
    Hello,
    Heartbeat,
    Stop,
    CallPermit,
    HelloAck,
    Event,
    Stopped,
    ReadArtifact,
    BeginInput,
    FileChunk,
    CommitInput,
    InputAccepted,
    CommitArtifact,
    ArtifactCommitted,
    InferenceRequest,
    InferenceResponse,
    Cancel,
}

impl MessageKind {
    fn parse(value: &str) -> Result<Self, FrameError> {
        match value {
            "Hello" => Ok(Self::Hello),
            "Heartbeat" => Ok(Self::Heartbeat),
            "Stop" => Ok(Self::Stop),
            "CallPermit" => Ok(Self::CallPermit),
            "HelloAck" => Ok(Self::HelloAck),
            "Event" => Ok(Self::Event),
            "Stopped" => Ok(Self::Stopped),
            "ReadArtifact" => Ok(Self::ReadArtifact),
            "BeginInput" => Ok(Self::BeginInput),
            "FileChunk" => Ok(Self::FileChunk),
            "CommitInput" => Ok(Self::CommitInput),
            "InputAccepted" => Ok(Self::InputAccepted),
            "CommitArtifact" => Ok(Self::CommitArtifact),
            "ArtifactCommitted" => Ok(Self::ArtifactCommitted),
            "InferenceRequest" => Ok(Self::InferenceRequest),
            "InferenceResponse" => Ok(Self::InferenceResponse),
            "Cancel" => Ok(Self::Cancel),
            _ => Err(FrameError::MessageType),
        }
    }

    fn allowed(self, channel: Channel, direction: Direction) -> bool {
        use Direction::{GuestToHost as G, HostToGuest as H};
        matches!(
            (channel, direction, self),
            (
                Channel::Control,
                H,
                Self::Hello | Self::Heartbeat | Self::Stop | Self::CallPermit
            ) | (
                Channel::Control,
                G,
                Self::HelloAck | Self::Event | Self::Stopped
            ) | (
                Channel::File,
                H,
                Self::ReadArtifact
                    | Self::BeginInput
                    | Self::FileChunk
                    | Self::CommitInput
                    | Self::ArtifactCommitted
            ) | (
                Channel::File,
                G,
                Self::FileChunk | Self::InputAccepted | Self::CommitArtifact
            ) | (
                Channel::Inference,
                H,
                Self::InferenceResponse | Self::Cancel
            ) | (Channel::Inference, G, Self::InferenceRequest)
        )
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum FrameError {
    Header(HeaderError),
    Io,
    Hash,
    Json,
    MessageType,
    Direction,
    ZeroRequest,
    UnknownRequest,
    DuplicateRequest,
    Backpressure,
}

impl From<HeaderError> for FrameError {
    fn from(value: HeaderError) -> Self {
        Self::Header(value)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Frame {
    pub header: Header,
    pub payload: Vec<u8>,
    pub kind: MessageKind,
}

impl Frame {
    pub fn new(
        channel: Channel,
        epoch: u64,
        request: [u8; 16],
        payload: Vec<u8>,
        direction: Direction,
    ) -> Result<Self, FrameError> {
        if request == [0; 16] {
            return Err(FrameError::ZeroRequest);
        }
        if payload.len() as u64 > channel.payload_limit() {
            return Err(FrameError::Header(HeaderError::PayloadLimit));
        }
        let kind = parse_payload(&payload, channel, direction)?;
        let header = Header {
            channel,
            epoch,
            request,
            payload_length: payload.len() as u32,
            payload_sha256: sha256::digest(&payload),
        };
        Ok(Self {
            header,
            payload,
            kind,
        })
    }

    pub fn decode(
        header_bytes: &[u8],
        payload: Vec<u8>,
        expected_epoch: u64,
        direction: Direction,
    ) -> Result<Self, FrameError> {
        let header = Header::parse(header_bytes, expected_epoch)?;
        if payload.len() != header.payload_length as usize {
            return Err(FrameError::Header(HeaderError::Size));
        }
        if sha256::digest(&payload) != header.payload_sha256 {
            return Err(FrameError::Hash);
        }
        if header.request == [0; 16] {
            return Err(FrameError::ZeroRequest);
        }
        let kind = parse_payload(&payload, header.channel, direction)?;
        Ok(Self {
            header,
            payload,
            kind,
        })
    }

    pub fn bytes(&self) -> Vec<u8> {
        let mut bytes = Vec::with_capacity(HEADER_BYTES + self.payload.len());
        bytes.extend_from_slice(&self.header.encode());
        bytes.extend_from_slice(&self.payload);
        bytes
    }

    pub fn string_field(&self, name: &str) -> Result<Option<String>, FrameError> {
        let fields = json::top_level_strings(&self.payload).map_err(|_| FrameError::Json)?;
        Ok(fields.get(name).cloned())
    }
}

fn parse_payload(
    payload: &[u8],
    channel: Channel,
    direction: Direction,
) -> Result<MessageKind, FrameError> {
    let fields = json::top_level_strings(payload).map_err(|_| FrameError::Json)?;
    let kind = MessageKind::parse(fields.get("type").expect("JSON validator requires type"))?;
    if !kind.allowed(channel, direction) {
        return Err(FrameError::Direction);
    }
    Ok(kind)
}

pub struct FrameReader<R> {
    inner: R,
    expected_epoch: u64,
    expected_channel: Channel,
    direction: Direction,
}

impl<R: Read> FrameReader<R> {
    pub fn new(
        inner: R,
        expected_epoch: u64,
        expected_channel: Channel,
        direction: Direction,
    ) -> Self {
        Self {
            inner,
            expected_epoch,
            expected_channel,
            direction,
        }
    }

    pub fn read(&mut self) -> Result<Frame, FrameError> {
        let mut header_bytes = [0; HEADER_BYTES];
        self.inner
            .read_exact(&mut header_bytes)
            .map_err(|_| FrameError::Io)?;
        let header = Header::parse(&header_bytes, self.expected_epoch)?;
        if header.channel != self.expected_channel {
            return Err(FrameError::Direction);
        }
        let mut payload = vec![0; header.payload_length as usize];
        self.inner
            .read_exact(&mut payload)
            .map_err(|_| FrameError::Io)?;
        Frame::decode(&header_bytes, payload, self.expected_epoch, self.direction)
    }
}

pub struct RequestRegistry {
    local_pending: HashSet<[u8; 16]>,
    local_seen: HashSet<[u8; 16]>,
    remote: HashSet<[u8; 16]>,
    maximum: usize,
}

impl RequestRegistry {
    pub fn new(maximum: usize) -> Result<Self, FrameError> {
        if maximum == 0 || maximum > 4096 {
            return Err(FrameError::Backpressure);
        }
        Ok(Self {
            local_pending: HashSet::new(),
            local_seen: HashSet::new(),
            remote: HashSet::new(),
            maximum,
        })
    }

    pub fn register_local(&mut self, request: [u8; 16]) -> Result<(), FrameError> {
        if request == [0; 16] {
            return Err(FrameError::Backpressure);
        }
        if self.local_seen.contains(&request) || self.remote.contains(&request) {
            return Err(FrameError::DuplicateRequest);
        }
        if self.local_pending.len() >= self.maximum || self.local_seen.len() >= self.maximum {
            return Err(FrameError::Backpressure);
        }
        self.local_seen.insert(request);
        self.local_pending.insert(request);
        Ok(())
    }

    pub fn accept_remote(
        &mut self,
        frame: &Frame,
        responds_to_local: bool,
    ) -> Result<(), FrameError> {
        let request = frame.header.request;
        if responds_to_local {
            if !self.local_pending.remove(&request) {
                return Err(FrameError::UnknownRequest);
            }
        } else {
            if self.local_seen.contains(&request) {
                return Err(FrameError::DuplicateRequest);
            }
            if self.remote.len() >= self.maximum {
                return Err(FrameError::Backpressure);
            }
            if !self.remote.insert(request) {
                return Err(FrameError::DuplicateRequest);
            }
        }
        Ok(())
    }

    pub fn complete_remote(&mut self, request: [u8; 16]) -> Result<(), FrameError> {
        if self.remote.contains(&request) {
            Ok(())
        } else {
            Err(FrameError::UnknownRequest)
        }
    }
}

pub struct ChannelQueue {
    channel: Channel,
    frames: VecDeque<Frame>,
    queued_bytes: u64,
    limit: u64,
}

impl ChannelQueue {
    pub fn new(channel: Channel) -> Self {
        let limit = match channel {
            Channel::Control => 4 * channel.payload_limit(),
            Channel::File => 2 * channel.payload_limit(),
            Channel::Inference => 2 * channel.payload_limit(),
        };
        Self {
            channel,
            frames: VecDeque::new(),
            queued_bytes: 0,
            limit,
        }
    }

    pub fn enqueue(&mut self, frame: Frame) -> Result<(), FrameError> {
        if frame.header.channel != self.channel {
            return Err(FrameError::Direction);
        }
        let size = HEADER_BYTES as u64 + frame.payload.len() as u64;
        let next = self
            .queued_bytes
            .checked_add(size)
            .ok_or(FrameError::Backpressure)?;
        if next > self.limit {
            return Err(FrameError::Backpressure);
        }
        self.queued_bytes = next;
        self.frames.push_back(frame);
        Ok(())
    }

    pub fn dequeue(&mut self) -> Option<Frame> {
        let frame = self.frames.pop_front()?;
        self.queued_bytes -= HEADER_BYTES as u64 + frame.payload.len() as u64;
        Some(frame)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn frame(kind: &str, request: u8, direction: Direction) -> Frame {
        Frame::new(
            Channel::Control,
            7,
            [request; 16],
            format!(r#"{{"type":"{kind}"}}"#).into_bytes(),
            direction,
        )
        .unwrap()
    }

    #[test]
    fn full_frame_hash_json_direction_and_stream() {
        let original = frame("Hello", 1, Direction::HostToGuest);
        let bytes = original.bytes();
        let mut reader = FrameReader::new(
            Cursor::new(bytes.clone()),
            7,
            Channel::Control,
            Direction::HostToGuest,
        );
        assert_eq!(reader.read().unwrap(), original);

        let mut bad_hash = bytes.clone();
        *bad_hash.last_mut().unwrap() ^= 1;
        let mut reader = FrameReader::new(
            Cursor::new(bad_hash),
            7,
            Channel::Control,
            Direction::HostToGuest,
        );
        assert_eq!(reader.read(), Err(FrameError::Hash));

        let inference = Frame::new(
            Channel::Inference,
            7,
            [3; 16],
            br#"{"type":"InferenceResponse"}"#.to_vec(),
            Direction::HostToGuest,
        )
        .unwrap();
        let mut reader = FrameReader::new(
            Cursor::new(inference.bytes()),
            7,
            Channel::Control,
            Direction::HostToGuest,
        );
        assert_eq!(reader.read(), Err(FrameError::Direction));
        assert_eq!(
            Frame::new(
                Channel::Control,
                7,
                [2; 16],
                br#"{"type":"Hello","type":"Stop"}"#.to_vec(),
                Direction::HostToGuest,
            ),
            Err(FrameError::Json)
        );
        assert_eq!(
            Frame::new(
                Channel::Control,
                7,
                [2; 16],
                br#"{"type":"HelloAck"}"#.to_vec(),
                Direction::HostToGuest,
            ),
            Err(FrameError::Direction)
        );
    }

    #[test]
    fn request_registry_rejects_unknown_and_replay() {
        let mut registry = RequestRegistry::new(2).unwrap();
        registry.register_local([1; 16]).unwrap();
        assert_eq!(
            registry.register_local([1; 16]),
            Err(FrameError::DuplicateRequest)
        );
        let reply = frame("HelloAck", 1, Direction::GuestToHost);
        registry.accept_remote(&reply, true).unwrap();
        assert_eq!(
            registry.accept_remote(&reply, true),
            Err(FrameError::UnknownRequest)
        );
        assert_eq!(
            registry.register_local([1; 16]),
            Err(FrameError::DuplicateRequest)
        );
        let event = frame("Event", 2, Direction::GuestToHost);
        registry.accept_remote(&event, false).unwrap();
        assert_eq!(
            registry.accept_remote(&event, false),
            Err(FrameError::DuplicateRequest)
        );
        registry.complete_remote([2; 16]).unwrap();
        assert_eq!(
            registry.accept_remote(&event, false),
            Err(FrameError::DuplicateRequest)
        );

        let mut registry = RequestRegistry::new(2).unwrap();
        registry.register_local([3; 16]).unwrap();
        let collision = frame("Event", 3, Direction::GuestToHost);
        assert_eq!(
            registry.accept_remote(&collision, false),
            Err(FrameError::DuplicateRequest)
        );
    }

    #[test]
    fn independent_queues_apply_backpressure() {
        let control = frame("Stop", 1, Direction::HostToGuest);
        let mut control_queue = ChannelQueue::new(Channel::Control);
        for _ in 0..4_000 {
            if control_queue.enqueue(control.clone()).is_err() {
                break;
            }
        }
        assert_eq!(
            control_queue.enqueue(control.clone()),
            Err(FrameError::Backpressure)
        );
        let inference = Frame::new(
            Channel::Inference,
            7,
            [3; 16],
            br#"{"type":"InferenceResponse"}"#.to_vec(),
            Direction::HostToGuest,
        )
        .unwrap();
        let mut inference_queue = ChannelQueue::new(Channel::Inference);
        inference_queue.enqueue(inference).unwrap();
        assert!(inference_queue.dequeue().is_some());
        assert!(control_queue.dequeue().is_some());
    }

    #[test]
    fn oversized_and_cross_epoch_frames_fail_before_payload_allocation() {
        let frame = frame("Hello", 1, Direction::HostToGuest);
        let mut oversized = frame.header.encode();
        oversized[32..36].copy_from_slice(&65_537_u32.to_be_bytes());
        let mut reader = FrameReader::new(
            Cursor::new(oversized),
            7,
            Channel::Control,
            Direction::HostToGuest,
        );
        assert_eq!(
            reader.read(),
            Err(FrameError::Header(HeaderError::PayloadLimit))
        );

        let mut reader = FrameReader::new(
            Cursor::new(frame.bytes()),
            8,
            Channel::Control,
            Direction::HostToGuest,
        );
        assert_eq!(reader.read(), Err(FrameError::Header(HeaderError::Epoch)));
    }
}
