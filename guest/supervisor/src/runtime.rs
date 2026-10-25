use crate::{Error, Role, SyntheticCase};
#[cfg(target_os = "linux")]
use crate::{Supervisor, TaskCgroup, process::SandboxLauncher};

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use hb_protocol::{Channel, Direction, Frame, FrameReader, MessageKind, RequestRegistry};
    use std::fs::File;
    use std::io::Write;
    use std::os::fd::AsRawFd;
    use std::sync::mpsc::{Receiver, RecvTimeoutError, SyncSender, TrySendError, sync_channel};
    use std::time::{Duration, Instant};

    pub fn run(
        instance: [u8; 16],
        epoch: u64,
        role: Role,
        test_case: SyntheticCase,
    ) -> Result<(), Error> {
        let result = run_inner(instance, epoch, role, test_case);
        if let Err(error) = result {
            return match TaskCgroup::fixed().kill_and_wait_empty(Duration::from_secs(3)) {
                Ok(()) => Err(error),
                Err(cleanup) => Err(cleanup),
            };
        }
        result
    }

    fn run_inner(
        instance: [u8; 16],
        epoch: u64,
        role: Role,
        test_case: SyntheticCase,
    ) -> Result<(), Error> {
        crate::inspect_live_boot()?;
        let launcher = SandboxLauncher::start()?;
        let mut broker = BrokerGuard::new(launcher.broker_pid()?, launcher.broker_control()?);
        let crate::virtio_channel::Channels {
            control,
            file,
            inference,
        } = crate::virtio_channel::open_fixed_channels()?;
        let writer = writer(control.try_clone().map_err(crate::io)?);
        let control_rx = reader(control, epoch, Channel::Control, Direction::HostToGuest, 16);
        let file_rx = reader(file, epoch, Channel::File, Direction::HostToGuest, 2);
        let inference_rx = reader(
            inference,
            epoch,
            Channel::Inference,
            Direction::HostToGuest,
            2,
        );
        let start = Instant::now();
        let mut supervisor = Supervisor::new(instance, epoch, 0)?;
        let mut requests = RequestRegistry::new(4096).map_err(|_| Error::Protocol)?;
        let instance_text = hex(&instance);
        let hello = receive_until_tick(&control_rx, &mut supervisor, &start)?;
        if hello.kind != MessageKind::Hello
            || hello
                .string_field("instance")
                .map_err(|_| Error::Protocol)?
                != Some(instance_text.clone())
        {
            return Err(Error::Handshake);
        }
        requests
            .accept_remote(&hello, false)
            .map_err(|_| Error::Protocol)?;
        supervisor.hello(instance, epoch, elapsed_ms(&start))?;
        send(
            &writer,
            Frame::new(
                Channel::Control,
                epoch,
                hello.header.request,
                format!(r#"{{"type":"HelloAck","instance":"{instance_text}"}}"#).into_bytes(),
                Direction::GuestToHost,
            )
            .map_err(|_| Error::Protocol)?,
        )?;
        requests
            .complete_remote(hello.header.request)
            .map_err(|_| Error::Protocol)?;

        let (task_tx, task_rx) = sync_channel(1);
        std::thread::spawn(move || {
            let mut launcher = launcher;
            let result = launcher.launch(role, test_case);
            let _ = task_tx.send((result, launcher));
        });
        let mut sequence = 0_u64;
        let mut task_done = false;
        loop {
            reject_side_channel(&file_rx)?;
            reject_side_channel(&inference_rx)?;
            if !task_done {
                if let Ok((result, _launcher)) = task_rx.try_recv() {
                    let output = result?;
                    broker.terminate()?;
                    task_done = true;
                    let request = next_request(hello.header.request, 1);
                    send(
                        &writer,
                        Frame::new(
                            Channel::Control,
                            epoch,
                            request,
                            format!(
                                r#"{{"type":"Event","event":"SyntheticTaskCompleted","stdoutBytes":"{}","stderrBytes":"{}"}}"#,
                                output.stdout.len(),
                                output.stderr.len()
                            )
                            .into_bytes(),
                            Direction::GuestToHost,
                        )
                        .map_err(|_| Error::Protocol)?,
                    )?;
                }
            }
            match control_rx.recv_timeout(Duration::from_millis(25)) {
                Ok(Ok(frame)) => {
                    requests
                        .accept_remote(&frame, false)
                        .map_err(|_| Error::Protocol)?;
                    match frame.kind {
                        MessageKind::Heartbeat => {
                            let received = frame
                                .string_field("sequence")
                                .map_err(|_| Error::Protocol)?
                                .and_then(|value| value.parse::<u64>().ok())
                                .ok_or(Error::Protocol)?;
                            if received <= sequence {
                                return Err(Error::Replay);
                            }
                            sequence = received;
                            supervisor.renew(sequence, elapsed_ms(&start))?;
                            requests
                                .complete_remote(frame.header.request)
                                .map_err(|_| Error::Protocol)?;
                        }
                        MessageKind::Stop => {
                            supervisor.interrupt();
                            broker.terminate()?;
                            TaskCgroup::fixed().kill_and_wait_empty(Duration::from_secs(3))?;
                            sync_data()?;
                            send(
                                &writer,
                                Frame::new(
                                    Channel::Control,
                                    epoch,
                                    frame.header.request,
                                    br#"{"type":"Stopped"}"#.to_vec(),
                                    Direction::GuestToHost,
                                )
                                .map_err(|_| Error::Protocol)?,
                            )?;
                            return Ok(());
                        }
                        _ => return Err(Error::Protocol),
                    }
                }
                Ok(Err(_)) | Err(RecvTimeoutError::Disconnected) => {
                    return Err(Error::Protocol);
                }
                Err(RecvTimeoutError::Timeout) => {
                    if supervisor.tick(elapsed_ms(&start)).is_err() {
                        return Err(Error::Lease);
                    }
                }
            }
        }
    }

    fn reader(
        file: File,
        epoch: u64,
        channel: Channel,
        direction: Direction,
        capacity: usize,
    ) -> Receiver<Result<Frame, hb_protocol::FrameError>> {
        let (sender, receiver) = sync_channel(capacity);
        std::thread::spawn(move || read_frames(file, epoch, channel, direction, sender));
        receiver
    }

    fn read_frames(
        file: File,
        epoch: u64,
        channel: Channel,
        direction: Direction,
        sender: SyncSender<Result<Frame, hb_protocol::FrameError>>,
    ) {
        let mut reader = FrameReader::new(file, epoch, channel, direction);
        loop {
            let result = reader.read();
            let failed = result.is_err();
            if sender.send(result).is_err() || failed {
                return;
            }
        }
    }

    fn receive_until_tick(
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

    fn reject_side_channel(
        receiver: &Receiver<Result<Frame, hb_protocol::FrameError>>,
    ) -> Result<(), Error> {
        match receiver.try_recv() {
            Err(std::sync::mpsc::TryRecvError::Empty) => Ok(()),
            _ => Err(Error::Protocol),
        }
    }

    type WriteRequest = (Frame, SyncSender<Result<(), Error>>);

    fn writer(mut file: File) -> SyncSender<WriteRequest> {
        let (sender, receiver) = sync_channel::<WriteRequest>(4);
        std::thread::spawn(move || {
            while let Ok((frame, acknowledgement)) = receiver.recv() {
                let result = file
                    .write_all(&frame.bytes())
                    .and_then(|_| file.flush())
                    .map_err(crate::io);
                let failed = result.is_err();
                let _ = acknowledgement.send(result);
                if failed {
                    return;
                }
            }
        });
        sender
    }

    fn send(writer: &SyncSender<WriteRequest>, frame: Frame) -> Result<(), Error> {
        let (acknowledgement, result) = sync_channel(1);
        match writer.try_send((frame, acknowledgement)) {
            Ok(()) => {}
            Err(TrySendError::Full(_)) => return Err(Error::Resource),
            Err(TrySendError::Disconnected(_)) => return Err(Error::Protocol),
        }
        result
            .recv_timeout(Duration::from_secs(1))
            .map_err(|_| Error::Resource)?
    }

    struct BrokerGuard {
        pid: Option<u32>,
        control: File,
    }

    impl BrokerGuard {
        fn new(pid: u32, control: File) -> Self {
            Self {
                pid: Some(pid),
                control,
            }
        }

        fn terminate(&mut self) -> Result<(), Error> {
            if let Some(pid) = self.pid {
                crate::process::terminate_broker(&mut self.control, pid)?;
                self.pid = None;
            }
            Ok(())
        }
    }

    impl Drop for BrokerGuard {
        fn drop(&mut self) {
            let _ = self.terminate();
        }
    }

    fn elapsed_ms(start: &Instant) -> u64 {
        u64::try_from(start.elapsed().as_millis()).unwrap_or(u64::MAX)
    }

    fn sync_data() -> Result<(), Error> {
        unsafe extern "C" {
            fn syncfs(descriptor: std::ffi::c_int) -> std::ffi::c_int;
        }
        let data = File::open("/data").map_err(crate::io)?;
        if unsafe { syncfs(data.as_raw_fd()) } == 0 {
            Ok(())
        } else {
            Err(crate::io(std::io::Error::last_os_error()))
        }
    }

    fn hex(bytes: &[u8; 16]) -> String {
        bytes.iter().map(|byte| format!("{byte:02x}")).collect()
    }

    fn next_request(mut request: [u8; 16], increment: u8) -> [u8; 16] {
        request[15] = request[15].wrapping_add(increment);
        if request == [0; 16] { [1; 16] } else { request }
    }
}

pub fn run(
    instance: [u8; 16],
    epoch: u64,
    role: Role,
    test_case: SyntheticCase,
) -> Result<(), Error> {
    #[cfg(target_os = "linux")]
    {
        linux::run(instance, epoch, role, test_case)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (instance, epoch, role, test_case);
        Err(Error::Unsupported)
    }
}
