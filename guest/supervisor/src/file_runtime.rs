//! Fixed MVP-02 file-channel runtime. It exercises file staging and return only;
//! it does not admit a CLI command, model request, or arbitrary executable.
use crate::task_channel::{Binding, FileError, FileLimits, TaskFiles, fail_closed_root};
use crate::{Error, Supervisor, TaskCgroup};
use hb_protocol::{
    Channel, Direction, FileVersionId, Frame, FrameReader, MessageKind, RequestRegistry, Sha256,
    TaskId,
};
use std::fs::File;
use std::io::Write;
use std::sync::mpsc::{Receiver, RecvTimeoutError, SyncSender, TryRecvError, sync_channel};
use std::time::{Duration, Instant};

const ACTIVE_ROOT: &str = "/data/active";
// Base64 expands by 4/3. This leaves more than 2 KiB for the JSON envelope
// while guaranteeing the complete payload fits hb.channel.v1's 262144 bytes.
const ARTIFACT_CHUNK_BYTES: usize = 190 * 1024;

enum FileSession {
    Idle,
    Receiving(FileVersionId),
    ArtifactPending {
        id: FileVersionId,
        request: [u8; 16],
        bytes: Vec<u8>,
    },
    ArtifactAcked,
}

impl FileSession {
    fn has_unacknowledged_artifact(&self) -> bool {
        matches!(self, Self::ArtifactPending { .. })
    }
}

pub fn run(instance: [u8; 16], epoch: u64) -> Result<(), Error> {
    fail_closed_root(std::path::Path::new(ACTIVE_ROOT), &mut TaskCgroup::fixed())
        .map_err(file_error)?;
    let result = run_inner(instance, epoch);
    let cleanup = fail_closed_root(std::path::Path::new(ACTIVE_ROOT), &mut TaskCgroup::fixed())
        .map_err(file_error);
    if cleanup.is_ok() {
        eprintln!("MVP02_FAIL_CLOSED cgroup_empty=true incoming_empty=true tmp_empty=true");
    }
    result.and(cleanup)
}

fn run_inner(instance: [u8; 16], epoch: u64) -> Result<(), Error> {
    crate::inspect_live_boot()?;
    let crate::virtio_channel::Channels {
        control,
        file,
        inference,
    } = crate::virtio_channel::open_fixed_channels()?;
    let control_writer = writer(control.try_clone().map_err(crate::io)?);
    let file_writer = writer(file.try_clone().map_err(crate::io)?);
    let control_rx = reader(control, epoch, Channel::Control);
    let file_rx = reader(file, epoch, Channel::File);
    let inference_rx = reader(inference, epoch, Channel::Inference);
    let start = Instant::now();
    let mut supervisor = Supervisor::new(instance, epoch, 0)?;
    let task = TaskId::parse("task_mvp02").map_err(|_| Error::Protocol)?;
    let mut files = TaskFiles::open(
        std::path::Path::new(ACTIVE_ROOT),
        epoch,
        task.clone(),
        0,
        FileLimits::default(),
    )
    .map_err(file_error)?;

    let hello = receive_control(&control_rx, &mut supervisor, &start)?;
    if hello.kind != MessageKind::Hello
        || hello
            .string_field("instance")
            .map_err(|_| Error::Protocol)?
            != Some(hex(&instance))
    {
        return Err(Error::Handshake);
    }
    supervisor.hello(instance, epoch, elapsed_ms(&start))?;
    send(
        &control_writer,
        Frame::new(
            Channel::Control,
            epoch,
            hello.header.request,
            format!(r#"{{"type":"HelloAck","instance":"{}"}}"#, hex(&instance)).into_bytes(),
            Direction::GuestToHost,
        )
        .map_err(|_| Error::Protocol)?,
    )?;

    let mut sequence = 0_u64;
    let mut session = FileSession::Idle;
    let mut pending_stop = None;
    let mut requests = RequestRegistry::new(4096).map_err(|_| Error::Protocol)?;
    let mut local_sequence = 1_u64;
    loop {
        if !matches!(inference_rx.try_recv(), Err(TryRecvError::Empty)) {
            return Err(Error::Protocol);
        }
        match control_rx.try_recv() {
            Ok(Ok(frame)) if frame.kind == MessageKind::Heartbeat => {
                let received = required(&frame, "sequence")?
                    .parse::<u64>()
                    .map_err(|_| Error::Protocol)?;
                if received <= sequence {
                    return Err(Error::Replay);
                }
                sequence = received;
                supervisor.renew(sequence, elapsed_ms(&start))?;
                files
                    .heartbeat(binding(&task, &frame), sequence, elapsed_ms(&start))
                    .map_err(file_error)?;
            }
            Ok(Ok(frame)) if frame.kind == MessageKind::Stop => {
                if let Some(stop) = accept_stop(&session, &mut pending_stop, frame)? {
                    finish_stop(
                        stop,
                        epoch,
                        &task,
                        &mut supervisor,
                        &mut files,
                        &control_writer,
                    )?;
                    return Ok(());
                }
            }
            Ok(Ok(_)) | Ok(Err(_)) | Err(TryRecvError::Disconnected) => {
                return Err(Error::Protocol);
            }
            Err(TryRecvError::Empty) => {}
        }
        match file_rx.try_recv() {
            Ok(Ok(frame)) => {
                accept_file_request(&pending_stop, &mut requests, &frame)?;
                match frame.kind {
                    MessageKind::BeginInput => {
                        if !matches!(session, FileSession::Idle) {
                            return Err(Error::Protocol);
                        }
                        if required(&frame, "mediaType")? != "text/csv" {
                            return Err(Error::Boundary);
                        }
                        let id = file_id(&frame)?;
                        let size = required(&frame, "size")?
                            .parse::<u64>()
                            .map_err(|_| Error::Protocol)?;
                        let hash = digest(&required(&frame, "sha256")?)?;
                        files
                            .begin_input(binding(&task, &frame), id.clone(), size, hash)
                            .map_err(file_error)?;
                        session = FileSession::Receiving(id);
                    }
                    MessageKind::FileChunk => {
                        let id = file_id(&frame)?;
                        if !matches!(&session, FileSession::Receiving(expected) if expected == &id)
                        {
                            return Err(Error::Protocol);
                        }
                        let offset = required(&frame, "offset")?
                            .parse::<u64>()
                            .map_err(|_| Error::Protocol)?;
                        let bytes = base64_decode(&required(&frame, "bytes")?)?;
                        files
                            .input_chunk(binding(&task, &frame), &id, offset, &bytes)
                            .map_err(file_error)?;
                    }
                    MessageKind::CommitInput => {
                        let id = file_id(&frame)?;
                        if !matches!(&session, FileSession::Receiving(expected) if expected == &id)
                        {
                            return Err(Error::Protocol);
                        }
                        let input = files
                            .commit_input(binding(&task, &frame), &id)
                            .map_err(file_error)?;
                        let metadata = std::fs::metadata(&input).map_err(|error| {
                            file_error(FileError::io("runtime-input-metadata", error))
                        })?;
                        if metadata.permissions().readonly() == false {
                            return Err(file_error(FileError::staged("runtime-input-readonly")));
                        }
                        let bytes = std::fs::read(&input).map_err(|error| {
                            file_error(FileError::io("runtime-input-read", error))
                        })?;
                        let hash = sha(&bytes);
                        send(&file_writer, Frame::new(
                        Channel::File, epoch, frame.header.request,
                        format!(r#"{{"type":"InputAccepted","fileId":"{}","size":"{}","sha256":"{}","readOnly":"true"}}"#, id.as_str(), bytes.len(), hex_digest(&hash)).into_bytes(),
                        Direction::GuestToHost,
                    ).map_err(|_| Error::Protocol)?)?;
                        let artifact_id = FileVersionId::parse("file_mvp02-result")
                            .map_err(|_| Error::Protocol)?;
                        let temporary_name = "mvp02-result.csv";
                        std::fs::write(
                            std::path::Path::new("/data/active/tmp").join(temporary_name),
                            &bytes,
                        )
                        .map_err(|error| {
                            file_error(FileError::io("runtime-artifact-write", error))
                        })?;
                        let request = local_request(local_sequence);
                        local_sequence = local_sequence.checked_add(1).ok_or(Error::Resource)?;
                        requests
                            .register_local(request)
                            .map_err(|_| Error::Protocol)?;
                        let artifact_path = files
                            .commit_artifact(
                                Binding {
                                    epoch,
                                    task: &task,
                                    request,
                                },
                                &artifact_id,
                                temporary_name,
                                "text/csv",
                                bytes.len() as u64,
                                hash,
                            )
                            .map_err(file_error)?;
                        let returned = std::fs::read(&artifact_path).map_err(|error| {
                            file_error(FileError::io("runtime-artifact-read", error))
                        })?;
                        if returned != bytes
                            || !std::fs::metadata(&artifact_path)
                                .map_err(|error| {
                                    file_error(FileError::io("runtime-artifact-metadata", error))
                                })?
                                .permissions()
                                .readonly()
                        {
                            return Err(file_error(FileError::staged("runtime-artifact-verify")));
                        }
                        send(&file_writer, Frame::new(
                        Channel::File, epoch, request,
                        format!(r#"{{"type":"CommitArtifact","fileId":"{}","size":"{}","sha256":"{}","mediaType":"text/csv"}}"#, artifact_id.as_str(), returned.len(), hex_digest(&hash)).into_bytes(),
                        Direction::GuestToHost,
                    ).map_err(|_| Error::Protocol)?)?;
                        session = FileSession::ArtifactPending {
                            id: artifact_id,
                            request,
                            bytes: returned,
                        };
                    }
                    MessageKind::ReadArtifact => {
                        let id = file_id(&frame)?;
                        let offset = required(&frame, "offset")?
                            .parse::<usize>()
                            .map_err(|_| Error::Protocol)?;
                        let (expected, bytes) = match &session {
                            FileSession::ArtifactPending { id, bytes, .. } => (id, bytes),
                            _ => return Err(Error::Protocol),
                        };
                        if &id != expected || offset > bytes.len() {
                            return Err(Error::Boundary);
                        }
                        let payload = artifact_chunk_payload(&id, offset, bytes)?;
                        send(
                            &file_writer,
                            Frame::new(
                                Channel::File,
                                epoch,
                                frame.header.request,
                                payload,
                                Direction::GuestToHost,
                            )
                            .map_err(|_| Error::Protocol)?,
                        )?;
                    }
                    MessageKind::ArtifactCommitted => {
                        if let Some(stop) =
                            accept_artifact_committed(&mut session, &mut pending_stop, &frame)?
                        {
                            finish_stop(
                                stop,
                                epoch,
                                &task,
                                &mut supervisor,
                                &mut files,
                                &control_writer,
                            )?;
                            return Ok(());
                        }
                    }
                    _ => return Err(Error::Protocol),
                }
            }
            Ok(Err(_)) | Err(TryRecvError::Disconnected) => return Err(Error::Protocol),
            Err(TryRecvError::Empty) => {}
        }
        supervisor.tick(elapsed_ms(&start))?;
        std::thread::sleep(Duration::from_millis(5));
    }
}

fn accept_stop(
    session: &FileSession,
    pending_stop: &mut Option<Frame>,
    frame: Frame,
) -> Result<Option<Frame>, Error> {
    if pending_stop.is_some() {
        return Err(Error::Protocol);
    }
    if session.has_unacknowledged_artifact() {
        // Control and file are independent virtio queues. Stop may be observed
        // before the already-sent artifact acknowledgment, so retain it as a
        // barrier without acknowledging or cleaning up early.
        *pending_stop = Some(frame);
        return Ok(None);
    }
    Ok(Some(frame))
}

fn accept_file_request(
    pending_stop: &Option<Frame>,
    requests: &mut RequestRegistry,
    frame: &Frame,
) -> Result<(), Error> {
    if pending_stop.is_some() && frame.kind != MessageKind::ArtifactCommitted {
        return Err(Error::Protocol);
    }
    requests
        .accept_remote(frame, frame.kind == MessageKind::ArtifactCommitted)
        .map_err(|_| Error::Replay)
}

fn accept_artifact_committed(
    session: &mut FileSession,
    pending_stop: &mut Option<Frame>,
    frame: &Frame,
) -> Result<Option<Frame>, Error> {
    let id = file_id(frame)?;
    match session {
        FileSession::ArtifactPending {
            id: expected,
            request,
            ..
        } if expected == &id && request == &frame.header.request => {}
        _ => return Err(Error::Protocol),
    }
    *session = FileSession::ArtifactAcked;
    Ok(pending_stop.take())
}

fn finish_stop(
    frame: Frame,
    epoch: u64,
    task: &TaskId,
    supervisor: &mut Supervisor,
    files: &mut TaskFiles,
    control_writer: &SyncSender<WriteRequest>,
) -> Result<(), Error> {
    supervisor.interrupt();
    files
        .stop(binding(task, &frame), &mut TaskCgroup::fixed())
        .map_err(file_error)?;
    sync_data()?;
    send(
        control_writer,
        Frame::new(
            Channel::Control,
            epoch,
            frame.header.request,
            br#"{"type":"Stopped","taskProcesses":"0"}"#.to_vec(),
            Direction::GuestToHost,
        )
        .map_err(|_| Error::Protocol)?,
    )
}

fn binding<'a>(task: &'a TaskId, frame: &Frame) -> Binding<'a> {
    Binding {
        epoch: frame.header.epoch,
        task,
        request: frame.header.request,
    }
}
fn required(frame: &Frame, name: &str) -> Result<String, Error> {
    frame
        .string_field(name)
        .map_err(|_| Error::Protocol)?
        .ok_or(Error::Protocol)
}
fn file_id(frame: &Frame) -> Result<FileVersionId, Error> {
    FileVersionId::parse(&required(frame, "fileId")?).map_err(|_| Error::Protocol)
}
fn file_error_marker(error: &FileError) -> String {
    let errno = error
        .raw_os_error()
        .map_or_else(|| "none".to_string(), |value| value.to_string());
    format!("HB_FILE_ERROR stage={} errno={errno}", error.stage())
}
fn map_file_error(error: FileError, mut diagnostic: impl Write) -> Error {
    let _ = writeln!(diagnostic, "{}", file_error_marker(&error));
    Error::Boundary
}
fn file_error(error: FileError) -> Error {
    map_file_error(error, std::io::stderr().lock())
}
fn sha(bytes: &[u8]) -> [u8; 32] {
    let mut value = Sha256::new();
    value.update(bytes);
    value.finish()
}
fn digest(text: &str) -> Result<[u8; 32], Error> {
    if text.len() != 64 {
        return Err(Error::Protocol);
    }
    let mut result = [0_u8; 32];
    for (index, byte) in result.iter_mut().enumerate() {
        *byte =
            u8::from_str_radix(&text[index * 2..index * 2 + 2], 16).map_err(|_| Error::Protocol)?;
    }
    Ok(result)
}
fn hex_digest(bytes: &[u8; 32]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
fn hex(bytes: &[u8; 16]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}
fn local_request(sequence: u64) -> [u8; 16] {
    let mut request = [0_u8; 16];
    request[0] = 0x80;
    request[8..].copy_from_slice(&sequence.to_be_bytes());
    request
}
fn elapsed_ms(start: &Instant) -> u64 {
    u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX)
}

fn reader(
    file: File,
    epoch: u64,
    channel: Channel,
) -> Receiver<Result<Frame, hb_protocol::FrameError>> {
    let (sender, receiver) = sync_channel(4);
    std::thread::spawn(move || {
        let mut reader = FrameReader::new(file, epoch, channel, Direction::HostToGuest);
        loop {
            let result = reader.read();
            let failed = result.is_err();
            if sender.send(result).is_err() || failed {
                return;
            }
        }
    });
    receiver
}
type WriteRequest = (Frame, SyncSender<Result<(), Error>>);
fn writer(mut file: File) -> SyncSender<WriteRequest> {
    let (sender, receiver) = sync_channel::<WriteRequest>(4);
    std::thread::spawn(move || {
        while let Ok((frame, ack)) = receiver.recv() {
            let result = file
                .write_all(&frame.bytes())
                .and_then(|_| file.flush())
                .map_err(crate::io);
            let failed = result.is_err();
            let _ = ack.send(result);
            if failed {
                return;
            }
        }
    });
    sender
}
fn send(writer: &SyncSender<WriteRequest>, frame: Frame) -> Result<(), Error> {
    let (tx, rx) = sync_channel(1);
    writer.send((frame, tx)).map_err(|_| Error::Protocol)?;
    rx.recv_timeout(Duration::from_secs(1))
        .map_err(|_| Error::Resource)?
}
fn receive_control(
    receiver: &Receiver<Result<Frame, hb_protocol::FrameError>>,
    supervisor: &mut Supervisor,
    start: &Instant,
) -> Result<Frame, Error> {
    loop {
        match receiver.recv_timeout(Duration::from_millis(25)) {
            Ok(Ok(frame)) => return Ok(frame),
            Ok(Err(_)) | Err(RecvTimeoutError::Disconnected) => return Err(Error::Protocol),
            Err(RecvTimeoutError::Timeout) => supervisor.tick(elapsed_ms(start))?,
        }
    }
}
fn sync_data() -> Result<(), Error> {
    File::open("/data")
        .and_then(|file| file.sync_all())
        .map_err(crate::io)
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
fn base64_encode(input: &[u8]) -> String {
    let mut result = String::new();
    for chunk in input.chunks(3) {
        let value = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        result.push(B64[((value >> 18) & 63) as usize] as char);
        result.push(B64[((value >> 12) & 63) as usize] as char);
        result.push(if chunk.len() > 1 {
            B64[((value >> 6) & 63) as usize] as char
        } else {
            '='
        });
        result.push(if chunk.len() > 2 {
            B64[(value & 63) as usize] as char
        } else {
            '='
        });
    }
    result
}

fn artifact_chunk_payload(
    id: &FileVersionId,
    offset: usize,
    bytes: &[u8],
) -> Result<Vec<u8>, Error> {
    if offset > bytes.len() {
        return Err(Error::Boundary);
    }
    let end = offset
        .checked_add(ARTIFACT_CHUNK_BYTES)
        .map(|end| end.min(bytes.len()))
        .ok_or(Error::Resource)?;
    let payload = format!(
        r#"{{"type":"FileChunk","fileId":"{}","offset":"{}","bytes":"{}","eof":"{}"}}"#,
        id.as_str(),
        offset,
        base64_encode(&bytes[offset..end]),
        end == bytes.len()
    )
    .into_bytes();
    if payload.len() as u64 > Channel::File.payload_limit() {
        return Err(Error::Resource);
    }
    Ok(payload)
}
fn base64_decode(input: &str) -> Result<Vec<u8>, Error> {
    if input.len() % 4 != 0 {
        return Err(Error::Protocol);
    }
    let mut result = Vec::with_capacity(input.len() / 4 * 3);
    for chunk in input.as_bytes().chunks_exact(4) {
        let a = b64(chunk[0])?;
        let b = b64(chunk[1])?;
        let c = if chunk[2] == b'=' { 0 } else { b64(chunk[2])? };
        let d = if chunk[3] == b'=' { 0 } else { b64(chunk[3])? };
        if chunk[2] == b'=' && chunk[3] != b'=' {
            return Err(Error::Protocol);
        }
        let value =
            (u32::from(a) << 18) | (u32::from(b) << 12) | (u32::from(c) << 6) | u32::from(d);
        result.push((value >> 16) as u8);
        if chunk[2] != b'=' {
            result.push((value >> 8) as u8);
        }
        if chunk[3] != b'=' {
            result.push(value as u8);
        }
    }
    Ok(result)
}
fn b64(byte: u8) -> Result<u8, Error> {
    B64.iter()
        .position(|value| *value == byte)
        .map(|value| value as u8)
        .ok_or(Error::Protocol)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn host_frame(channel: Channel, request: [u8; 16], payload: &[u8]) -> Frame {
        Frame::new(
            channel,
            23,
            request,
            payload.to_vec(),
            Direction::HostToGuest,
        )
        .unwrap()
    }

    fn pending_session() -> FileSession {
        FileSession::ArtifactPending {
            id: FileVersionId::parse("file_mvp02-result").unwrap(),
            request: local_request(1),
            bytes: b"result".to_vec(),
        }
    }

    fn stop_frame(request: [u8; 16]) -> Frame {
        host_frame(
            Channel::Control,
            request,
            br#"{"type":"Stop","task":"task_mvp02"}"#,
        )
    }

    fn artifact_ack(request: [u8; 16], file_id: &str) -> Frame {
        host_frame(
            Channel::File,
            request,
            format!(r#"{{"type":"ArtifactCommitted","fileId":"{file_id}"}}"#).as_bytes(),
        )
    }

    #[test]
    fn stop_waits_for_a_matching_artifact_ack_when_both_channels_are_ready() {
        let artifact_request = local_request(1);
        let stop_request = [0x22; 16];
        let mut session = pending_session();
        let mut pending_stop = None;
        let stop = stop_frame(stop_request);
        let mut requests = RequestRegistry::new(4).unwrap();
        requests.register_local(artifact_request).unwrap();

        assert_eq!(
            accept_stop(&session, &mut pending_stop, stop).unwrap(),
            None
        );
        assert_eq!(pending_stop.as_ref().unwrap().header.request, stop_request);

        let ack = artifact_ack(artifact_request, "file_mvp02-result");
        accept_file_request(&pending_stop, &mut requests, &ack).unwrap();
        let released = accept_artifact_committed(&mut session, &mut pending_stop, &ack)
            .unwrap()
            .unwrap();
        assert!(matches!(session, FileSession::ArtifactAcked));
        assert_eq!(released.header.request, stop_request);
    }

    #[test]
    fn artifact_ack_then_stop_and_stop_then_ack_release_the_same_stop() {
        let artifact_request = local_request(1);
        let stop_request = [0x33; 16];

        let mut ack_first_session = pending_session();
        let mut ack_first_stop = None;
        let mut ack_first_requests = RequestRegistry::new(4).unwrap();
        ack_first_requests.register_local(artifact_request).unwrap();
        let ack = artifact_ack(artifact_request, "file_mvp02-result");
        accept_file_request(&ack_first_stop, &mut ack_first_requests, &ack).unwrap();
        assert!(
            accept_artifact_committed(&mut ack_first_session, &mut ack_first_stop, &ack)
                .unwrap()
                .is_none()
        );
        let ack_first = accept_stop(
            &ack_first_session,
            &mut ack_first_stop,
            stop_frame(stop_request),
        )
        .unwrap()
        .unwrap();

        let mut stop_first_session = pending_session();
        let mut stop_first_stop = None;
        let mut stop_first_requests = RequestRegistry::new(4).unwrap();
        stop_first_requests
            .register_local(artifact_request)
            .unwrap();
        assert!(
            accept_stop(
                &stop_first_session,
                &mut stop_first_stop,
                stop_frame(stop_request),
            )
            .unwrap()
            .is_none()
        );
        accept_file_request(&stop_first_stop, &mut stop_first_requests, &ack).unwrap();
        let stop_first =
            accept_artifact_committed(&mut stop_first_session, &mut stop_first_stop, &ack)
                .unwrap()
                .unwrap();

        assert_eq!(ack_first.header.request, stop_first.header.request);
        assert_eq!(stop_first.header.request, stop_request);
        assert!(matches!(ack_first_session, FileSession::ArtifactAcked));
        assert!(matches!(stop_first_session, FileSession::ArtifactAcked));
    }

    #[test]
    fn pending_stop_rejects_invalid_ack_replay_second_stop_and_new_file_work() {
        let artifact_request = local_request(1);
        let stop_request = [0x44; 16];

        for ack in [
            artifact_ack(local_request(2), "file_mvp02-result"),
            artifact_ack(artifact_request, "file_wrong"),
        ] {
            let mut session = pending_session();
            let mut pending_stop = None;
            let mut requests = RequestRegistry::new(4).unwrap();
            requests.register_local(artifact_request).unwrap();
            assert!(
                accept_stop(&session, &mut pending_stop, stop_frame(stop_request))
                    .unwrap()
                    .is_none()
            );
            let result = accept_file_request(&pending_stop, &mut requests, &ack)
                .and_then(|()| accept_artifact_committed(&mut session, &mut pending_stop, &ack))
                .map(|stop| stop.map(|frame| frame.header.request));
            assert!(matches!(result, Err(Error::Replay | Error::Protocol)));
            assert_eq!(pending_stop.unwrap().header.request, stop_request);
        }

        let session = pending_session();
        let mut pending_stop = None;
        assert!(
            accept_stop(&session, &mut pending_stop, stop_frame(stop_request))
                .unwrap()
                .is_none()
        );
        assert_eq!(
            accept_stop(&session, &mut pending_stop, stop_frame([0x45; 16])),
            Err(Error::Protocol)
        );

        let mut requests = RequestRegistry::new(4).unwrap();
        requests.register_local(artifact_request).unwrap();
        let begin = host_frame(
            Channel::File,
            [0x46; 16],
            br#"{"type":"BeginInput","fileId":"file_new","size":"0","sha256":"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855","mediaType":"text/csv"}"#,
        );
        assert_eq!(
            accept_file_request(&pending_stop, &mut requests, &begin),
            Err(Error::Protocol)
        );
        assert_eq!(pending_stop.unwrap().header.request, stop_request);

        let mut replay_session = pending_session();
        let mut no_stop = None;
        let mut replay_requests = RequestRegistry::new(4).unwrap();
        replay_requests.register_local(artifact_request).unwrap();
        let ack = artifact_ack(artifact_request, "file_mvp02-result");
        accept_file_request(&no_stop, &mut replay_requests, &ack).unwrap();
        assert!(
            accept_artifact_committed(&mut replay_session, &mut no_stop, &ack)
                .unwrap()
                .is_none()
        );
        assert_eq!(
            accept_file_request(&no_stop, &mut replay_requests, &ack),
            Err(Error::Replay)
        );
    }

    #[test]
    fn unacknowledged_pending_stop_emits_no_stop_action_and_lease_stays_terminal() {
        let instance = [0x11; 16];
        let mut supervisor = Supervisor::new(instance, 23, 0).unwrap();
        supervisor.hello(instance, 23, 1).unwrap();
        let session = pending_session();
        let mut pending_stop = None;

        assert!(
            accept_stop(&session, &mut pending_stop, stop_frame([0x55; 16]))
                .unwrap()
                .is_none()
        );
        supervisor.renew(1, 2).unwrap();
        assert_eq!(supervisor.tick(5_002), Err(Error::Lease));
        assert_eq!(supervisor.state(), &crate::State::Interrupted);
        assert!(pending_stop.is_some());
    }

    #[test]
    fn file_error_marker_is_stable_path_free_and_still_maps_to_boundary() {
        let error = crate::task_channel::FileError::io(
            "artifact-open",
            std::io::Error::from_raw_os_error(13),
        );
        let marker = file_error_marker(&error);
        assert_eq!(marker, "HB_FILE_ERROR stage=artifact-open errno=13");
        assert_eq!(marker.lines().count(), 1);
        assert!(!marker.contains("/data/active"));
        let mut diagnostic = Vec::new();
        assert_eq!(map_file_error(error, &mut diagnostic), Error::Boundary);
        assert_eq!(
            std::str::from_utf8(&diagnostic).unwrap(),
            "HB_FILE_ERROR stage=artifact-open errno=13\n"
        );

        let marker = file_error_marker(&crate::task_channel::FileError::staged(
            "artifact-hash-mismatch",
        ));
        assert_eq!(
            marker,
            "HB_FILE_ERROR stage=artifact-hash-mismatch errno=none"
        );
    }

    #[test]
    fn artifact_chunks_fit_frame_and_mark_exact_eof() {
        let id = FileVersionId::parse("file_artifact").unwrap();
        let bytes = vec![0x5a; ARTIFACT_CHUNK_BYTES * 2];
        let first = artifact_chunk_payload(&id, 0, &bytes).unwrap();
        assert!(first.len() as u64 <= Channel::File.payload_limit());
        assert!(
            std::str::from_utf8(&first)
                .unwrap()
                .contains(r#""eof":"false""#)
        );

        let second = artifact_chunk_payload(&id, ARTIFACT_CHUNK_BYTES, &bytes).unwrap();
        assert!(second.len() as u64 <= Channel::File.payload_limit());
        assert!(
            std::str::from_utf8(&second)
                .unwrap()
                .contains(r#""eof":"true""#)
        );

        let eof = artifact_chunk_payload(&id, bytes.len(), &bytes).unwrap();
        let eof = std::str::from_utf8(&eof).unwrap();
        assert!(eof.contains(r#""bytes":"""#));
        assert!(eof.contains(r#""eof":"true""#));
    }
}
