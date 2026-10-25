//! Unix candidate runner: bounded nonblocking pipes, monotonic deadline, process group.
//! Group disappearance is not Linux subreaper/cgroup proof; escaped descendants
//! remain a blocked runtime integration requirement.
use super::{Diagnostic, Limits, Result, error};
use std::io::Read;
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
unsafe extern "C" {
    fn fcntl(fd: i32, command: i32, ...) -> i32;
    fn kill(pid: i32, signal: i32) -> i32;
}
#[cfg(target_os = "macos")]
const NONBLOCK: i32 = 4;
#[cfg(target_os = "linux")]
const NONBLOCK: i32 = 2048;
pub(super) struct Output {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub status: i32,
}
fn nonblocking(fd: i32) -> Result<()> {
    // F_GETFL/F_SETFL preserve existing descriptor flags.
    let flags = unsafe { fcntl(fd, 3) };
    if flags < 0 || unsafe { fcntl(fd, 4, flags | NONBLOCK) } < 0 {
        return Err(error("pipe_flags"));
    }
    Ok(())
}
fn cleanup(child: &mut Child, group: i32) -> bool {
    unsafe {
        kill(-group, 9);
    }
    let deadline = Instant::now() + Duration::from_millis(250);
    loop {
        let reaped = matches!(child.try_wait(), Ok(Some(_)));
        let gone = unsafe { kill(-group, 0) } < 0
            && std::io::Error::last_os_error().raw_os_error() == Some(3);
        if reaped && gone {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}
fn read_once(
    reader: &mut impl Read,
    bytes: &mut Vec<u8>,
    total: &mut usize,
    limit: usize,
) -> Result<bool> {
    let mut chunk = [0; 4096];
    match reader.read(&mut chunk) {
        Ok(0) => Ok(true),
        Ok(n) => {
            *total += n;
            if *total > limit {
                return Err(error("output_limit"));
            }
            bytes.extend_from_slice(&chunk[..n]);
            Ok(false)
        }
        Err(e)
            if matches!(
                e.kind(),
                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
            ) =>
        {
            Ok(false)
        }
        Err(_) => Err(error("pipe_read")),
    }
}
pub(super) fn run(
    executable: &Path,
    cwd: &Path,
    argv: &[String],
    limits: &Limits,
    cancelled: &AtomicBool,
) -> Result<Output> {
    if cancelled.load(Ordering::Acquire) {
        return Err(error("cancelled"));
    }
    let deadline = Instant::now() + Duration::from_millis(limits.timeout_ms);
    let mut child = Command::new(executable)
        .args(argv)
        .current_dir(cwd)
        .env_clear()
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
        .map_err(|_| error("spawn"))?;
    let group = child.id() as i32;
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let mut out = Output {
        stdout: vec![],
        stderr: vec![],
        status: 128,
    };
    let result = (|| {
        nonblocking(stdout.as_raw_fd())?;
        nonblocking(stderr.as_raw_fd())?;
        let (mut out_eof, mut err_eof, mut exited, mut total) = (false, false, false, 0);
        loop {
            if cancelled.load(Ordering::Acquire) {
                return Err(error("cancelled"));
            }
            if Instant::now() >= deadline {
                return Err(error("timeout"));
            }
            if !out_eof {
                out_eof = read_once(&mut stdout, &mut out.stdout, &mut total, limits.total_bytes)?;
            }
            if !err_eof {
                err_eof = read_once(&mut stderr, &mut out.stderr, &mut total, limits.total_bytes)?;
            }
            if let Some(status) = child.try_wait().map_err(|_| error("wait"))? {
                out.status = status.code().unwrap_or(128);
                exited = true;
            }
            if exited && out_eof && err_eof {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(1));
        }
    })();
    // Also kill lingering descendants after a normal parent exit. Never wait
    // without a deadline on inherited pipes or claim cleanup on uncertainty.
    let confirmed = cleanup(&mut child, group);
    if let Err(e) = result {
        return Err(Diagnostic {
            code: e.code,
            status: child
                .try_wait()
                .ok()
                .flatten()
                .map(|s| s.code().unwrap_or(128)),
            stdout_bytes: out.stdout.len(),
            stderr_bytes: out.stderr.len(),
            cleanup_confirmed: confirmed,
        });
    }
    if !confirmed {
        return Err(Diagnostic {
            code: "cleanup_unconfirmed",
            status: Some(out.status),
            stdout_bytes: out.stdout.len(),
            stderr_bytes: out.stderr.len(),
            cleanup_confirmed: false,
        });
    }
    Ok(out)
}
