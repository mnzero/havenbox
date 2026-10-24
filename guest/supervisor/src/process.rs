#[cfg(target_os = "linux")]
use crate::{Boundary, TaskCgroup};
use crate::{Error, Role, SyntheticCase};

#[derive(Debug)]
pub struct TaskOutput {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

#[derive(Clone, Copy)]
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
enum Stream {
    Stdout,
    Stderr,
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
struct CapturedOutput {
    output: TaskOutput,
    limits: crate::policy::StreamLimits,
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
impl CapturedOutput {
    fn new(limits: crate::policy::StreamLimits) -> Self {
        Self {
            output: TaskOutput {
                stdout: Vec::with_capacity(limits.stdout_bytes.min(65_536)),
                stderr: Vec::with_capacity(limits.stderr_bytes.min(65_536)),
            },
            limits,
        }
    }

    fn push(&mut self, stream: Stream, bytes: &[u8]) -> Result<(), ()> {
        let (output, limit) = match stream {
            Stream::Stdout => (&mut self.output.stdout, self.limits.stdout_bytes),
            Stream::Stderr => (&mut self.output.stderr, self.limits.stderr_bytes),
        };
        if bytes.len() > limit.saturating_sub(output.len()) {
            return Err(());
        }
        output.extend_from_slice(bytes);
        Ok(())
    }

    fn finish(self) -> TaskOutput {
        self.output
    }
}

#[cfg(any(target_os = "linux", test))]
fn successful_exit_status(status: i32) -> bool {
    status & 0x7f == 0 && (status >> 8) & 0xff == 0
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use crate::policy::{StreamLimits, SyntheticProfile};
    use std::ffi::{CString, c_char, c_int, c_short, c_void};
    use std::fs::File;
    use std::io::{Read, Write};
    use std::os::fd::{AsRawFd, FromRawFd, RawFd};
    use std::time::Duration;

    #[repr(C)]
    struct PollFd {
        fd: c_int,
        events: c_short,
        revents: c_short,
    }

    unsafe extern "C" {
        fn pipe2(pipefd: *mut c_int, flags: c_int) -> c_int;
        fn fork() -> c_int;
        fn close(fd: c_int) -> c_int;
        fn dup2(oldfd: c_int, newfd: c_int) -> c_int;
        fn fcntl(fd: c_int, command: c_int, ...) -> c_int;
        fn open(path: *const c_char, flags: c_int, ...) -> c_int;
        fn poll(fds: *mut PollFd, count: usize, timeout_ms: c_int) -> c_int;
        fn read(fd: c_int, buffer: *mut c_void, count: usize) -> isize;
        fn write(fd: c_int, buffer: *const c_void, count: usize) -> isize;
        fn waitpid(pid: c_int, status: *mut c_int, options: c_int) -> c_int;
        fn kill(pid: c_int, signal: c_int) -> c_int;
        fn execve(
            path: *const c_char,
            argv: *const *const c_char,
            envp: *const *const c_char,
        ) -> c_int;
        fn _exit(status: c_int) -> !;
    }

    const O_CLOEXEC: c_int = 0x80000;
    const O_NONBLOCK: c_int = 0x800;
    const O_RDWR: c_int = 2;
    const F_SETFL: c_int = 4;
    const POLLIN: c_short = 0x001;
    const POLLERR: c_short = 0x008;
    const POLLHUP: c_short = 0x010;
    const WNOHANG: c_int = 1;
    const SIGKILL: c_int = 9;
    const REPORT_BYTES: usize = 9;

    #[derive(Clone, Copy)]
    enum ReportKind {
        Completed = 0,
        Failed = 1,
        OutputExceeded = 2,
        CleanupFailed = 3,
    }

    struct Report {
        kind: ReportKind,
        output: TaskOutput,
    }

    pub struct Launcher {
        command: File,
        status: File,
        broker_pid: u32,
    }

    impl Launcher {
        pub fn start() -> Result<Self, Error> {
            let mut commands = [0; 2];
            let mut statuses = [0; 2];
            checked(unsafe { pipe2(commands.as_mut_ptr(), O_CLOEXEC) })?;
            if let Err(error) = checked(unsafe { pipe2(statuses.as_mut_ptr(), O_CLOEXEC) }) {
                close_pair(commands);
                return Err(error);
            }
            let pid = unsafe { fork() };
            if pid < 0 {
                close_pair(commands);
                close_pair(statuses);
                return Err(last_error());
            }
            if pid == 0 {
                unsafe {
                    close(commands[1]);
                    close(statuses[0]);
                }
                broker(commands[0], statuses[1]);
            }
            unsafe {
                close(commands[0]);
                close(statuses[1]);
            }
            Ok(Self {
                command: unsafe { File::from_raw_fd(commands[1]) },
                status: unsafe { File::from_raw_fd(statuses[0]) },
                broker_pid: pid as u32,
            })
        }

        pub fn launch(
            &mut self,
            role: Role,
            test_case: SyntheticCase,
        ) -> Result<TaskOutput, Error> {
            self.command
                .write_all(&[match (role, test_case) {
                    (Role::Harness, SyntheticCase::Normal) => b'H',
                    (Role::Worker, SyntheticCase::Normal) => b'W',
                    (Role::Harness, SyntheticCase::OutputOverflow) => b'O',
                    (Role::Worker, SyntheticCase::OutputOverflow) => b'P',
                    (Role::Harness, SyntheticCase::Hold) => b'L',
                    (Role::Worker, SyntheticCase::Hold) => b'K',
                }])
                .map_err(crate::io)?;
            let mut header = [0; REPORT_BYTES];
            self.status.read_exact(&mut header).map_err(crate::io)?;
            let kind = match header[0] {
                0 => ReportKind::Completed,
                1 => ReportKind::Failed,
                2 => ReportKind::OutputExceeded,
                3 => ReportKind::CleanupFailed,
                _ => return Err(Error::Protocol),
            };
            let stdout_length = u32::from_be_bytes(header[1..5].try_into().unwrap()) as usize;
            let stderr_length = u32::from_be_bytes(header[5..9].try_into().unwrap()) as usize;
            let limits = SyntheticProfile::for_role(role).stream_limits();
            if stdout_length > limits.stdout_bytes || stderr_length > limits.stderr_bytes {
                return Err(Error::Protocol);
            }
            let mut stdout = vec![0; stdout_length];
            let mut stderr = vec![0; stderr_length];
            self.status.read_exact(&mut stdout).map_err(crate::io)?;
            self.status.read_exact(&mut stderr).map_err(crate::io)?;
            match kind {
                ReportKind::Completed => Ok(TaskOutput { stdout, stderr }),
                ReportKind::Failed => Err(Error::Boundary),
                ReportKind::OutputExceeded | ReportKind::CleanupFailed => Err(Error::Resource),
            }
        }

        pub fn broker_pid(&self) -> u32 {
            self.broker_pid
        }

        pub fn broker_control(&self) -> Result<File, Error> {
            self.command.try_clone().map_err(crate::io)
        }
    }

    fn broker(command_fd: RawFd, status_fd: RawFd) -> ! {
        if close_except(&[command_fd, status_fd]).is_err() {
            unsafe { _exit(125) }
        }
        loop {
            let mut command = [0];
            if read_retry(command_fd, &mut command) != Ok(1) {
                unsafe { _exit(0) }
            }
            let (role, test_case) = match command[0] {
                b'H' => (Role::Harness, SyntheticCase::Normal),
                b'W' => (Role::Worker, SyntheticCase::Normal),
                b'O' => (Role::Harness, SyntheticCase::OutputOverflow),
                b'P' => (Role::Worker, SyntheticCase::OutputOverflow),
                b'L' => (Role::Harness, SyntheticCase::Hold),
                b'K' => (Role::Worker, SyntheticCase::Hold),
                b'X' => unsafe { _exit(0) },
                _ => unsafe { _exit(125) },
            };
            let report = match run_one(role, test_case, command_fd) {
                Ok(output) => Report {
                    kind: ReportKind::Completed,
                    output,
                },
                Err(RunFailure::Task(output)) => Report {
                    kind: ReportKind::Failed,
                    output,
                },
                Err(RunFailure::Output(output)) => Report {
                    kind: ReportKind::OutputExceeded,
                    output,
                },
                Err(RunFailure::Cleanup) => Report {
                    kind: ReportKind::CleanupFailed,
                    output: empty_output(),
                },
            };
            let cleanup_failed = matches!(report.kind, ReportKind::CleanupFailed);
            if send_report(status_fd, report).is_err() {
                unsafe { _exit(125) }
            }
            if cleanup_failed {
                unsafe { _exit(125) }
            }
        }
    }

    enum RunFailure {
        Task(TaskOutput),
        Output(TaskOutput),
        Cleanup,
    }

    fn run_one(
        role: Role,
        test_case: SyntheticCase,
        command_fd: RawFd,
    ) -> Result<TaskOutput, RunFailure> {
        let profile = SyntheticProfile::for_role(role);
        let boundary = profile.boundary();
        let cgroup = TaskCgroup::fixed();
        if cgroup.empty() != Ok(true) || cgroup.configure(boundary).is_err() {
            return Err(RunFailure::Cleanup);
        }
        let mut output_quota = match profile.output_limit() {
            Some(limit) => Some(
                crate::quota::WorkerOutputQuota::prepare(limit).map_err(|_| RunFailure::Cleanup)?,
            ),
            None => None,
        };
        let mut gate = [0; 2];
        let mut stdout = [0; 2];
        let mut stderr = [0; 2];
        if checked(unsafe { pipe2(gate.as_mut_ptr(), O_CLOEXEC) }).is_err() {
            return Err(RunFailure::Cleanup);
        }
        if checked(unsafe { pipe2(stdout.as_mut_ptr(), O_CLOEXEC) }).is_err() {
            close_pair(gate);
            return Err(RunFailure::Cleanup);
        }
        if checked(unsafe { pipe2(stderr.as_mut_ptr(), O_CLOEXEC) }).is_err() {
            close_pair(gate);
            close_pair(stdout);
            return Err(RunFailure::Cleanup);
        }
        let pid = unsafe { fork() };
        if pid < 0 {
            close_pair(gate);
            close_pair(stdout);
            close_pair(stderr);
            return Err(RunFailure::Cleanup);
        }
        if pid == 0 {
            unsafe {
                close(gate[1]);
                close(stdout[0]);
                close(stderr[0]);
            }
            task_child(boundary, test_case, gate[0], stdout[1], stderr[1]);
        }
        unsafe {
            close(gate[0]);
            close(stdout[1]);
            close(stderr[1]);
        }
        if cgroup.admit(pid as u32).is_err() {
            unsafe {
                close(gate[1]);
            }
            kill_and_reap_unadmitted(pid);
            close_pair([stdout[0], stderr[0]]);
            if cgroup.empty() != Ok(true) {
                let _ = cgroup.kill_and_wait_empty(Duration::from_secs(3));
            }
            return Err(RunFailure::Cleanup);
        }
        let released = write_all_fd(gate[1], &[1]).is_ok();
        unsafe {
            close(gate[1]);
        }
        if !released {
            let cleaned = cleanup_admitted(&cgroup, pid, false).is_ok();
            close_pair([stdout[0], stderr[0]]);
            return Err(if cleaned {
                RunFailure::Task(empty_output())
            } else {
                RunFailure::Cleanup
            });
        }
        if unsafe { fcntl(stdout[0], F_SETFL, O_NONBLOCK) } < 0
            || unsafe { fcntl(stderr[0], F_SETFL, O_NONBLOCK) } < 0
        {
            let cleaned = cleanup_admitted(&cgroup, pid, false).is_ok();
            close_pair([stdout[0], stderr[0]]);
            return Err(if cleaned {
                RunFailure::Task(empty_output())
            } else {
                RunFailure::Cleanup
            });
        }
        let stdout = unsafe { File::from_raw_fd(stdout[0]) };
        let stderr = unsafe { File::from_raw_fd(stderr[0]) };
        let result = capture_and_reap(
            pid,
            command_fd,
            stdout,
            stderr,
            &cgroup,
            profile.stream_limits(),
        );
        if let Some(quota) = &mut output_quota
            && quota.restore().is_err()
        {
            return Err(RunFailure::Cleanup);
        }
        result
    }

    fn capture_and_reap(
        pid: c_int,
        command_fd: RawFd,
        mut stdout_file: File,
        mut stderr_file: File,
        cgroup: &TaskCgroup,
        limits: StreamLimits,
    ) -> Result<TaskOutput, RunFailure> {
        let mut output = CapturedOutput::new(limits);
        let (mut stdout_eof, mut stderr_eof, mut status) = (false, false, None);
        loop {
            if status.is_none() {
                let mut current = 0;
                let waited = unsafe { waitpid(pid, &mut current, WNOHANG) };
                if waited == pid {
                    status = Some(current);
                    if cgroup.empty() != Ok(true)
                        && cgroup.kill_and_wait_empty(Duration::from_secs(3)).is_err()
                    {
                        return Err(RunFailure::Cleanup);
                    }
                } else if waited < 0 && std::io::Error::last_os_error().raw_os_error() != Some(4) {
                    let _ = cleanup_admitted(cgroup, pid, false);
                    return Err(RunFailure::Cleanup);
                }
            }
            if status.is_some() && stdout_eof && stderr_eof {
                if cgroup.empty() != Ok(true)
                    && cgroup.kill_and_wait_empty(Duration::from_secs(3)).is_err()
                {
                    return Err(RunFailure::Cleanup);
                }
                let output = output.finish();
                return if status == Some(0) {
                    Ok(output)
                } else {
                    Err(RunFailure::Task(output))
                };
            }
            let mut descriptors = [
                PollFd {
                    fd: command_fd,
                    events: POLLIN,
                    revents: 0,
                },
                PollFd {
                    fd: if stdout_eof {
                        -1
                    } else {
                        stdout_file.as_raw_fd()
                    },
                    events: POLLIN,
                    revents: 0,
                },
                PollFd {
                    fd: if stderr_eof {
                        -1
                    } else {
                        stderr_file.as_raw_fd()
                    },
                    events: POLLIN,
                    revents: 0,
                },
            ];
            let polled = unsafe { poll(descriptors.as_mut_ptr(), descriptors.len(), 50) };
            if polled < 0 {
                if std::io::Error::last_os_error().raw_os_error() == Some(4) {
                    continue;
                }
                let _ = cleanup_admitted(cgroup, pid, status.is_some());
                return Err(RunFailure::Cleanup);
            }
            if descriptors[0].revents & (POLLIN | POLLHUP | POLLERR) != 0 {
                let cleaned = cleanup_admitted(cgroup, pid, status.is_some()).is_ok();
                return Err(if cleaned {
                    RunFailure::Task(output.finish())
                } else {
                    RunFailure::Cleanup
                });
            }
            match drain(&mut stdout_file, &mut output, Stream::Stdout) {
                Ok(eof) => stdout_eof |= eof,
                Err(DrainFailure::Exceeded) => {
                    let cleaned = cleanup_admitted(cgroup, pid, status.is_some()).is_ok();
                    return Err(if cleaned {
                        RunFailure::Output(output.finish())
                    } else {
                        RunFailure::Cleanup
                    });
                }
                Err(DrainFailure::Io) => {
                    let _ = cleanup_admitted(cgroup, pid, status.is_some());
                    return Err(RunFailure::Cleanup);
                }
            }
            match drain(&mut stderr_file, &mut output, Stream::Stderr) {
                Ok(eof) => stderr_eof |= eof,
                Err(DrainFailure::Exceeded) => {
                    let cleaned = cleanup_admitted(cgroup, pid, status.is_some()).is_ok();
                    return Err(if cleaned {
                        RunFailure::Output(output.finish())
                    } else {
                        RunFailure::Cleanup
                    });
                }
                Err(DrainFailure::Io) => {
                    let _ = cleanup_admitted(cgroup, pid, status.is_some());
                    return Err(RunFailure::Cleanup);
                }
            }
        }
    }

    enum DrainFailure {
        Exceeded,
        Io,
    }

    fn drain(
        file: &mut File,
        output: &mut CapturedOutput,
        stream: Stream,
    ) -> Result<bool, DrainFailure> {
        let mut buffer = [0; 16_384];
        for _ in 0..4 {
            match file.read(&mut buffer) {
                Ok(0) => return Ok(true),
                Ok(count) => {
                    output
                        .push(stream, &buffer[..count])
                        .map_err(|_| DrainFailure::Exceeded)?;
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => return Ok(false),
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => return Err(DrainFailure::Io),
            }
        }
        Ok(false)
    }

    fn task_child(
        boundary: &Boundary,
        test_case: SyntheticCase,
        gate: RawFd,
        stdout: RawFd,
        stderr: RawFd,
    ) -> ! {
        if close_except(&[gate, stdout, stderr]).is_err() {
            unsafe { _exit(125) }
        }
        let mut release = [0];
        if read_retry(gate, &mut release) != Ok(1) || release[0] != 1 {
            unsafe { _exit(125) }
        }
        unsafe {
            close(gate);
        }
        if redirect_standard_fds(stdout, stderr).is_err() {
            unsafe { _exit(125) }
        }
        let executable = CString::new("/bin/busybox").expect("fixed path");
        let arg0 = CString::new("busybox").expect("fixed argument");
        let arg1 = CString::new("sh").expect("fixed argument");
        let arg2 = CString::new("/bin/hb-synthetic-task").expect("fixed argument");
        let role = CString::new(match boundary.role {
            Role::Harness => "harness",
            Role::Worker => "worker",
        })
        .expect("fixed role");
        let test_case = CString::new(match test_case {
            SyntheticCase::Normal => "normal",
            SyntheticCase::OutputOverflow => "output-overflow",
            SyntheticCase::Hold => "hold",
        })
        .expect("fixed synthetic case");
        let argv = [
            arg0.as_ptr(),
            arg1.as_ptr(),
            arg2.as_ptr(),
            role.as_ptr(),
            test_case.as_ptr(),
            std::ptr::null(),
        ];
        let envp = [std::ptr::null()];
        if unsafe { crate::sandbox::enter(boundary) }.is_err()
            || crate::seccomp::install_synthetic().is_err()
        {
            unsafe { _exit(125) }
        }
        unsafe {
            execve(executable.as_ptr(), argv.as_ptr(), envp.as_ptr());
            _exit(125)
        }
    }

    fn redirect_standard_fds(stdout: RawFd, stderr: RawFd) -> Result<(), Error> {
        let path = CString::new("/dev/null").expect("fixed path");
        let stdin = unsafe { open(path.as_ptr(), O_RDWR) };
        if stdin < 0 {
            return Err(last_error());
        }
        for (source, target) in [(stdin, 0), (stdout, 1), (stderr, 2)] {
            if unsafe { dup2(source, target) } < 0 {
                return Err(last_error());
            }
        }
        for descriptor in [stdin, stdout, stderr] {
            if descriptor > 2 {
                unsafe {
                    close(descriptor);
                }
            }
        }
        Ok(())
    }

    fn cleanup_admitted(
        cgroup: &TaskCgroup,
        pid: c_int,
        already_reaped: bool,
    ) -> Result<(), Error> {
        cgroup.kill_and_wait_empty(Duration::from_secs(3))?;
        if !already_reaped {
            reap(pid)?;
        }
        Ok(())
    }

    fn kill_and_reap_unadmitted(pid: c_int) {
        unsafe {
            kill(pid, SIGKILL);
        }
        let _ = reap(pid);
    }

    fn reap(pid: c_int) -> Result<(), Error> {
        loop {
            let mut status = 0;
            let result = unsafe { waitpid(pid, &mut status, 0) };
            if result == pid {
                return Ok(());
            }
            let error = std::io::Error::last_os_error();
            match error.raw_os_error() {
                Some(4) => continue,
                Some(10) => return Ok(()),
                _ => return Err(Error::Io(error.to_string())),
            }
        }
    }

    fn empty_output() -> TaskOutput {
        TaskOutput {
            stdout: vec![],
            stderr: vec![],
        }
    }

    fn send_report(fd: RawFd, report: Report) -> Result<(), Error> {
        let stdout_length =
            u32::try_from(report.output.stdout.len()).map_err(|_| Error::Resource)?;
        let stderr_length =
            u32::try_from(report.output.stderr.len()).map_err(|_| Error::Resource)?;
        let mut header = Vec::with_capacity(REPORT_BYTES);
        header.push(report.kind as u8);
        header.extend_from_slice(&stdout_length.to_be_bytes());
        header.extend_from_slice(&stderr_length.to_be_bytes());
        write_all_fd(fd, &header)?;
        write_all_fd(fd, &report.output.stdout)?;
        write_all_fd(fd, &report.output.stderr)
    }

    fn write_all_fd(fd: RawFd, mut bytes: &[u8]) -> Result<(), Error> {
        while !bytes.is_empty() {
            let count = unsafe { write(fd, bytes.as_ptr().cast(), bytes.len()) };
            if count > 0 {
                bytes = &bytes[count as usize..];
            } else if count < 0 && std::io::Error::last_os_error().raw_os_error() == Some(4) {
                continue;
            } else {
                return Err(last_error());
            }
        }
        Ok(())
    }

    fn read_retry(fd: RawFd, buffer: &mut [u8]) -> Result<usize, ()> {
        loop {
            let count = unsafe { read(fd, buffer.as_mut_ptr().cast(), buffer.len()) };
            if count >= 0 {
                return Ok(count as usize);
            }
            if std::io::Error::last_os_error().raw_os_error() != Some(4) {
                return Err(());
            }
        }
    }

    fn close_pair(pair: [RawFd; 2]) {
        unsafe {
            close(pair[0]);
            close(pair[1]);
        }
    }

    fn close_except(keep: &[RawFd]) -> Result<(), Error> {
        let entries = std::fs::read_dir("/proc/self/fd").map_err(crate::io)?;
        let mut descriptors = Vec::new();
        for entry in entries {
            let name = entry.map_err(crate::io)?.file_name();
            if let Ok(fd) = name.to_string_lossy().parse::<RawFd>() {
                descriptors.push(fd);
            }
        }
        for fd in descriptors {
            if !keep.contains(&fd) {
                unsafe {
                    close(fd);
                }
            }
        }
        Ok(())
    }

    fn checked(result: c_int) -> Result<(), Error> {
        if result == 0 {
            Ok(())
        } else {
            Err(last_error())
        }
    }
    fn last_error() -> Error {
        Error::Io(std::io::Error::last_os_error().to_string())
    }

    pub use Launcher as PlatformLauncher;

    pub fn terminate_broker(control: &mut File, pid: u32) -> Result<(), Error> {
        if pid <= 1 {
            return Err(Error::Boundary);
        }
        // The broker owns child cleanup and quota restoration. Asking it to
        // stop preserves those invariants; SIGKILL would bypass both.
        let write_result = control.write_all(b"X");
        let reap_result = reap_broker(pid as c_int);
        if let Err(error) = reap_result {
            return Err(error);
        }
        match write_result {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::BrokenPipe => Ok(()),
            Err(error) => Err(crate::io(error)),
        }
    }

    fn reap_broker(pid: c_int) -> Result<(), Error> {
        loop {
            let mut status = 0;
            let result = unsafe { waitpid(pid, &mut status, 0) };
            if result == pid {
                return if successful_exit_status(status) {
                    Ok(())
                } else {
                    Err(Error::Resource)
                };
            }
            let error = std::io::Error::last_os_error();
            match error.raw_os_error() {
                Some(4) => continue,
                _ => return Err(Error::Io(error.to_string())),
            }
        }
    }
}

pub struct SandboxLauncher {
    #[cfg(target_os = "linux")]
    inner: linux::PlatformLauncher,
}

impl SandboxLauncher {
    pub fn start() -> Result<Self, Error> {
        #[cfg(target_os = "linux")]
        {
            Ok(Self {
                inner: linux::PlatformLauncher::start()?,
            })
        }
        #[cfg(not(target_os = "linux"))]
        {
            Err(Error::Unsupported)
        }
    }

    pub fn launch(&mut self, role: Role, test_case: SyntheticCase) -> Result<TaskOutput, Error> {
        #[cfg(target_os = "linux")]
        {
            self.inner.launch(role, test_case)
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = (role, test_case);
            Err(Error::Unsupported)
        }
    }

    pub fn broker_pid(&self) -> Result<u32, Error> {
        #[cfg(target_os = "linux")]
        {
            Ok(self.inner.broker_pid())
        }
        #[cfg(not(target_os = "linux"))]
        {
            Err(Error::Unsupported)
        }
    }

    pub fn broker_control(&self) -> Result<std::fs::File, Error> {
        #[cfg(target_os = "linux")]
        {
            self.inner.broker_control()
        }
        #[cfg(not(target_os = "linux"))]
        {
            Err(Error::Unsupported)
        }
    }
}

pub fn terminate_broker(control: &mut std::fs::File, pid: u32) -> Result<(), Error> {
    #[cfg(target_os = "linux")]
    {
        linux::terminate_broker(control, pid)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = (control, pid);
        Err(Error::Unsupported)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::policy::StreamLimits;

    #[test]
    fn capture_enforces_each_stream_at_the_first_extra_byte() {
        let mut capture = CapturedOutput::new(StreamLimits {
            stdout_bytes: 4,
            stderr_bytes: 3,
        });
        capture.push(Stream::Stdout, b"12").unwrap();
        capture.push(Stream::Stderr, b"abc").unwrap();
        capture.push(Stream::Stdout, b"34").unwrap();
        assert!(capture.push(Stream::Stdout, b"5").is_err());
        assert!(capture.push(Stream::Stderr, b"d").is_err());
        let output = capture.finish();
        assert_eq!(output.stdout, b"1234");
        assert_eq!(output.stderr, b"abc");
    }

    #[test]
    fn capture_counts_arbitrary_bytes_not_text() {
        let mut capture = CapturedOutput::new(StreamLimits {
            stdout_bytes: 2,
            stderr_bytes: 2,
        });
        capture.push(Stream::Stdout, &[0, 0xff]).unwrap();
        capture.push(Stream::Stderr, &[0x80, b'\n']).unwrap();
        let output = capture.finish();
        assert_eq!(output.stdout, [0, 0xff]);
        assert_eq!(output.stderr, [0x80, b'\n']);
    }

    #[test]
    fn broker_wait_status_requires_clean_exit() {
        assert!(successful_exit_status(0));
        assert!(!successful_exit_status(1 << 8));
        assert!(!successful_exit_status(9));
    }
}
