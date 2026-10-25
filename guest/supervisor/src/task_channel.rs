//! Guest-side file transaction and lifecycle boundary for one MVP task.
//!
//! Wire decoding happens in `hb-protocol`; this module accepts only typed IDs
//! and never accepts an absolute or relative host path.
use crate::{Error, TaskCgroup};
use hb_protocol::{FileVersionId, Sha256, TaskId};
use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

pub const FILE_CHUNK_BYTES: usize = 256 * 1024;
#[cfg(target_os = "macos")]
const O_CLOEXEC: i32 = 0x1000000;
#[cfg(target_os = "macos")]
const O_NOFOLLOW: i32 = 0x100;

#[cfg(any(test, all(target_os = "linux", target_arch = "aarch64")))]
pub(crate) const AARCH64_LINUX_DIRECTORY_OPEN_FLAGS: i32 =
    crate::aarch64_linux_open_flags::DIRECTORY_OPEN_FLAGS;

#[cfg(target_os = "linux")]
const NOFOLLOW_OPEN_FLAGS: i32 = crate::aarch64_linux_open_flags::NOFOLLOW_OPEN_FLAGS;
#[cfg(target_os = "macos")]
const NOFOLLOW_OPEN_FLAGS: i32 = O_CLOEXEC | O_NOFOLLOW;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FileLimits {
    pub input_bytes: u64,
    pub input_total_bytes: u64,
    pub input_files: usize,
    pub artifact_bytes: u64,
    pub artifact_total_bytes: u64,
    pub artifact_files: usize,
}

impl Default for FileLimits {
    fn default() -> Self {
        Self {
            input_bytes: 50 * 1024 * 1024,
            input_total_bytes: 200 * 1024 * 1024,
            input_files: 20,
            artifact_bytes: 50 * 1024 * 1024,
            artifact_total_bytes: 64 * 1024 * 1024,
            artifact_files: 20,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum FileError {
    Binding,
    Replay,
    State,
    Path,
    Quota,
    Order,
    Size,
    Hash,
    MediaType,
    Cleanup,
    Staged {
        stage: &'static str,
        raw_os_error: Option<i32>,
    },
}

impl FileError {
    pub(crate) fn staged(stage: &'static str) -> Self {
        Self::Staged {
            stage,
            raw_os_error: None,
        }
    }

    pub(crate) fn io(stage: &'static str, error: std::io::Error) -> Self {
        Self::Staged {
            stage,
            raw_os_error: error.raw_os_error(),
        }
    }

    pub(crate) const fn stage(&self) -> &'static str {
        match self {
            Self::Binding => "binding",
            Self::Replay => "replay",
            Self::State => "state",
            Self::Path => "path",
            Self::Quota => "quota",
            Self::Order => "order",
            Self::Size => "size",
            Self::Hash => "hash",
            Self::MediaType => "media-type",
            Self::Cleanup => "cleanup",
            Self::Staged { stage, .. } => stage,
        }
    }

    pub(crate) const fn raw_os_error(&self) -> Option<i32> {
        match self {
            Self::Staged { raw_os_error, .. } => *raw_os_error,
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lifecycle {
    Running,
    Stopping,
    Stopped,
    CleanupFailed,
    Interrupted,
}

pub struct Binding<'a> {
    pub epoch: u64,
    pub task: &'a TaskId,
    pub request: [u8; 16],
}

pub trait Cleanup {
    fn terminate_and_wait(&mut self) -> Result<(), Error>;
}

impl Cleanup for TaskCgroup {
    fn terminate_and_wait(&mut self) -> Result<(), Error> {
        self.kill_and_wait_empty(Duration::from_secs(3))
    }
}

struct Incoming {
    file: File,
    path: PathBuf,
    expected_size: u64,
    expected_hash: [u8; 32],
    written: u64,
    digest: Sha256,
}

pub struct TaskFiles {
    root: PathBuf,
    epoch: u64,
    task: TaskId,
    limits: FileLimits,
    state: Lifecycle,
    lease_deadline_ms: u64,
    heartbeat_sequence: u64,
    seen: HashSet<[u8; 16]>,
    incoming: HashMap<FileVersionId, Incoming>,
    input_bytes: u64,
    input_files: usize,
    artifact_bytes: u64,
    artifact_files: usize,
}

impl TaskFiles {
    pub fn open(
        root: &Path,
        epoch: u64,
        task: TaskId,
        now_ms: u64,
        limits: FileLimits,
    ) -> Result<Self, FileError> {
        if epoch == 0 || !root.is_absolute() {
            return Err(FileError::Binding);
        }
        secure_directory(root, true)?;
        for name in ["input", "incoming", "tmp", "artifacts"] {
            secure_directory(&root.join(name), true)?;
        }
        purge_regular_files(&root.join("incoming"))?;
        purge_regular_files(&root.join("tmp"))?;
        let (input_files, input_bytes) = inventory(&root.join("input"))?;
        let (artifact_files, artifact_bytes) = inventory(&root.join("artifacts"))?;
        if input_files > limits.input_files
            || input_bytes > limits.input_total_bytes
            || artifact_files > limits.artifact_files
            || artifact_bytes > limits.artifact_total_bytes
        {
            return Err(FileError::Quota);
        }
        Ok(Self {
            root: root.to_owned(),
            epoch,
            task,
            limits,
            state: Lifecycle::Running,
            lease_deadline_ms: now_ms.checked_add(5_000).ok_or(FileError::Binding)?,
            heartbeat_sequence: 0,
            seen: HashSet::new(),
            incoming: HashMap::new(),
            input_bytes,
            input_files,
            artifact_bytes,
            artifact_files,
        })
    }

    pub const fn state(&self) -> Lifecycle {
        self.state
    }

    pub fn heartbeat(
        &mut self,
        binding: Binding<'_>,
        sequence: u64,
        now_ms: u64,
    ) -> Result<(), FileError> {
        self.admit(binding)?;
        if sequence <= self.heartbeat_sequence || now_ms >= self.lease_deadline_ms {
            return Err(FileError::Replay);
        }
        self.heartbeat_sequence = sequence;
        self.lease_deadline_ms = now_ms.checked_add(5_000).ok_or(FileError::Binding)?;
        Ok(())
    }

    pub fn begin_input(
        &mut self,
        binding: Binding<'_>,
        id: FileVersionId,
        size: u64,
        hash: [u8; 32],
    ) -> Result<(), FileError> {
        self.admit(binding)?;
        if size > self.limits.input_bytes
            || self.input_files + self.incoming.len() >= self.limits.input_files
            || self
                .input_bytes
                .checked_add(self.pending_input_bytes()?)
                .and_then(|total| total.checked_add(size))
                .filter(|total| *total <= self.limits.input_total_bytes)
                .is_none()
        {
            return Err(FileError::Quota);
        }
        if self.incoming.contains_key(&id) || self.input_path(&id).exists() {
            return Err(FileError::Replay);
        }
        let path = self
            .root
            .join("incoming")
            .join(format!("{}.part", id.as_str()));
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .map_err(|error| FileError::io("input-create", error))?;
        self.incoming.insert(
            id,
            Incoming {
                file,
                path,
                expected_size: size,
                expected_hash: hash,
                written: 0,
                digest: Sha256::new(),
            },
        );
        Ok(())
    }

    pub fn input_chunk(
        &mut self,
        binding: Binding<'_>,
        id: &FileVersionId,
        offset: u64,
        bytes: &[u8],
    ) -> Result<(), FileError> {
        self.admit(binding)?;
        if bytes.len() > FILE_CHUNK_BYTES {
            return Err(FileError::Quota);
        }
        let incoming = self.incoming.get_mut(id).ok_or(FileError::State)?;
        if offset != incoming.written {
            return Err(FileError::Order);
        }
        let end = offset
            .checked_add(bytes.len() as u64)
            .filter(|end| *end <= incoming.expected_size)
            .ok_or(FileError::Size)?;
        incoming
            .file
            .write_all(bytes)
            .map_err(|error| FileError::io("input-write", error))?;
        incoming.digest.update(bytes);
        incoming.written = end;
        Ok(())
    }

    pub fn commit_input(
        &mut self,
        binding: Binding<'_>,
        id: &FileVersionId,
    ) -> Result<PathBuf, FileError> {
        self.admit(binding)?;
        let mut incoming = self
            .incoming
            .remove(id)
            .ok_or_else(|| FileError::staged("input-commit-state"))?;
        if incoming.written != incoming.expected_size {
            remove(&incoming.path);
            return Err(FileError::staged("input-size-mismatch"));
        }
        if incoming.digest.finish() != incoming.expected_hash {
            remove(&incoming.path);
            return Err(FileError::staged("input-hash-mismatch"));
        }
        incoming
            .file
            .flush()
            .map_err(|error| FileError::io("input-flush", error))?;
        incoming
            .file
            .sync_all()
            .map_err(|error| FileError::io("input-sync", error))?;
        incoming
            .file
            .set_permissions(std::fs::Permissions::from_mode(0o444))
            .map_err(|error| FileError::io("input-chmod", error))?;
        let target = self.input_path(id);
        publish_new(&incoming.path, &target)?;
        drop(incoming.file);
        sync_directory(
            target.parent().expect("fixed input parent"),
            "input-publish-directory-sync",
        )?;
        self.input_bytes += incoming.expected_size;
        self.input_files += 1;
        Ok(target)
    }

    /// Atomically publishes a tool-created direct child of the task tmp area.
    pub fn commit_artifact(
        &mut self,
        binding: Binding<'_>,
        id: &FileVersionId,
        temporary_name: &str,
        media_type: &str,
        expected_size: u64,
        expected_hash: [u8; 32],
    ) -> Result<PathBuf, FileError> {
        self.admit(binding)?;
        if !safe_name(temporary_name) {
            return Err(FileError::Path);
        }
        let extension = match media_type {
            "text/csv" => "csv",
            "text/markdown" => "md",
            _ => return Err(FileError::MediaType),
        };
        if expected_size > self.limits.artifact_bytes
            || self.artifact_files >= self.limits.artifact_files
            || self
                .artifact_bytes
                .checked_add(expected_size)
                .filter(|total| *total <= self.limits.artifact_total_bytes)
                .is_none()
        {
            return Err(FileError::Quota);
        }
        let target = self
            .root
            .join("artifacts")
            .join(format!("{}.{}", id.as_str(), extension));
        if target.exists() {
            return Err(FileError::staged("artifact-target-exists"));
        }
        let source = self.root.join("tmp").join(temporary_name);
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(NOFOLLOW_OPEN_FLAGS)
            .open(&source)
            .map_err(|error| FileError::io("artifact-open", error))?;
        let metadata = file
            .metadata()
            .map_err(|error| FileError::io("artifact-metadata", error))?;
        if !metadata.is_file() || metadata.nlink() != 1 || metadata.len() != expected_size {
            return Err(FileError::staged("artifact-identity"));
        }
        if hash_reader(&mut file)? != expected_hash {
            return Err(FileError::staged("artifact-hash-mismatch"));
        }
        file.set_permissions(std::fs::Permissions::from_mode(0o444))
            .map_err(|error| FileError::io("artifact-chmod", error))?;
        file.sync_all()
            .map_err(|error| FileError::io("artifact-sync", error))?;
        publish_open_file(&file, &source, &target)?;
        drop(file);
        sync_directory(
            target.parent().expect("fixed artifact parent"),
            "artifact-publish-directory-sync",
        )?;
        self.artifact_bytes += expected_size;
        self.artifact_files += 1;
        Ok(target)
    }

    pub fn stop(
        &mut self,
        binding: Binding<'_>,
        cleanup: &mut dyn Cleanup,
    ) -> Result<(), FileError> {
        self.admit(binding)?;
        self.state = Lifecycle::Stopping;
        self.finish_cleanup(cleanup, Lifecycle::Stopped)
    }

    pub fn disconnect(&mut self, cleanup: &mut dyn Cleanup) -> Result<(), FileError> {
        if self.state != Lifecycle::Running {
            return Err(FileError::State);
        }
        self.state = Lifecycle::Stopping;
        self.finish_cleanup(cleanup, Lifecycle::Interrupted)
    }

    pub fn tick(&mut self, now_ms: u64, cleanup: &mut dyn Cleanup) -> Result<(), FileError> {
        if self.state != Lifecycle::Running {
            return Err(FileError::State);
        }
        if now_ms < self.lease_deadline_ms {
            return Ok(());
        }
        self.state = Lifecycle::Stopping;
        self.finish_cleanup(cleanup, Lifecycle::Interrupted)
    }

    fn finish_cleanup(
        &mut self,
        cleanup: &mut dyn Cleanup,
        success: Lifecycle,
    ) -> Result<(), FileError> {
        let result = fail_closed_root(&self.root, cleanup);
        self.incoming.clear();
        if let Err(error) = result {
            self.state = Lifecycle::CleanupFailed;
            return Err(error);
        }
        self.state = success;
        Ok(())
    }

    fn admit(&mut self, binding: Binding<'_>) -> Result<(), FileError> {
        if self.state != Lifecycle::Running {
            return Err(FileError::State);
        }
        if binding.epoch != self.epoch || binding.task != &self.task || binding.request == [0; 16] {
            return Err(FileError::Binding);
        }
        if self.seen.contains(&binding.request) {
            return Err(FileError::Replay);
        }
        if self.seen.len() >= 4096 {
            return Err(FileError::Quota);
        }
        self.seen.insert(binding.request);
        Ok(())
    }

    fn pending_input_bytes(&self) -> Result<u64, FileError> {
        self.incoming.values().try_fold(0_u64, |total, item| {
            total
                .checked_add(item.expected_size)
                .ok_or(FileError::Quota)
        })
    }

    fn input_path(&self, id: &FileVersionId) -> PathBuf {
        self.root.join("input").join(id.as_str())
    }
}

/// Kill the fixed task cgroup and clear unpublished data on every runtime exit.
/// Both cleanup stages are attempted so a cgroup error cannot leave staged data.
pub fn fail_closed_root(root: &Path, cleanup: &mut dyn Cleanup) -> Result<(), FileError> {
    let process_result = cleanup.terminate_and_wait();
    let incoming_result = purge_entries(&root.join("incoming"));
    let tmp_result = purge_entries(&root.join("tmp"));
    if let Err(error) = incoming_result {
        return Err(error);
    }
    if let Err(error) = tmp_result {
        return Err(error);
    }
    process_result.map_err(|_| FileError::Cleanup)
}

fn safe_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
        && name != "."
        && name != ".."
}

fn secure_directory(path: &Path, create: bool) -> Result<(), FileError> {
    if create {
        std::fs::create_dir_all(path)
            .map_err(|error| FileError::io("secure-directory-create", error))?;
    }
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| FileError::io("secure-directory-metadata", error))?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(FileError::Path);
    }
    Ok(())
}

fn inventory(path: &Path) -> Result<(usize, u64), FileError> {
    let mut files = 0_usize;
    let mut bytes = 0_u64;
    for entry in std::fs::read_dir(path).map_err(|error| FileError::io("inventory-open", error))? {
        let entry = entry.map_err(|error| FileError::io("inventory-read", error))?;
        let metadata = std::fs::symlink_metadata(entry.path())
            .map_err(|error| FileError::io("inventory-metadata", error))?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(FileError::Path);
        }
        files = files.checked_add(1).ok_or(FileError::Quota)?;
        bytes = bytes.checked_add(metadata.len()).ok_or(FileError::Quota)?;
    }
    Ok((files, bytes))
}

fn purge_regular_files(path: &Path) -> Result<(), FileError> {
    purge_entries(path)
}

fn purge_entries(path: &Path) -> Result<(), FileError> {
    let entries = match std::fs::read_dir(path) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(FileError::io("cleanup-open", error)),
    };
    for entry in entries {
        let entry = entry.map_err(|error| FileError::io("cleanup-read", error))?;
        let metadata = std::fs::symlink_metadata(entry.path())
            .map_err(|error| FileError::io("cleanup-metadata", error))?;
        if metadata.is_dir() && !metadata.file_type().is_symlink() {
            std::fs::remove_dir_all(entry.path())
                .map_err(|error| FileError::io("cleanup-remove-directory", error))?;
        } else {
            std::fs::remove_file(entry.path())
                .map_err(|error| FileError::io("cleanup-remove-file", error))?;
        }
    }
    Ok(())
}

fn hash_reader(reader: &mut File) -> Result<[u8; 32], FileError> {
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|error| FileError::io("artifact-hash-read", error))?;
        if read == 0 {
            return Ok(digest.finish());
        }
        digest.update(&buffer[..read]);
    }
}

fn sync_directory(path: &Path, stage: &'static str) -> Result<(), FileError> {
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|error| FileError::io(stage, error))
}

fn publish_new(source: &Path, target: &Path) -> Result<(), FileError> {
    if let Err(error) = std::fs::hard_link(source, target) {
        return if target.exists() {
            Err(FileError::staged("input-publish-target-exists"))
        } else {
            Err(FileError::io("input-publish-link", error))
        };
    }
    std::fs::remove_file(source).map_err(|error| FileError::io("input-publish-unlink", error))?;
    Ok(())
}

fn publish_open_file(file: &File, source: &Path, target: &Path) -> Result<(), FileError> {
    #[cfg(target_os = "linux")]
    {
        use std::ffi::CString;
        use std::os::fd::AsRawFd;
        use std::os::raw::{c_char, c_int};

        const AT_EMPTY_PATH: c_int = 0x1000;
        unsafe extern "C" {
            fn linkat(
                olddirfd: c_int,
                oldpath: *const c_char,
                newdirfd: c_int,
                newpath: *const c_char,
                flags: c_int,
            ) -> c_int;
        }

        let parent = target
            .parent()
            .ok_or_else(|| FileError::staged("artifact-publish-target-parent"))?;
        let name = target
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or_else(|| FileError::staged("artifact-publish-target-name"))?;
        let directory = OpenOptions::new()
            .read(true)
            .custom_flags(AARCH64_LINUX_DIRECTORY_OPEN_FLAGS)
            .open(parent)
            .map_err(|error| FileError::io("artifact-publish-directory-open", error))?;
        let empty = CString::new("").expect("empty CString");
        let name =
            CString::new(name).map_err(|_| FileError::staged("artifact-publish-target-name"))?;
        let linked = unsafe {
            linkat(
                file.as_raw_fd(),
                empty.as_ptr(),
                directory.as_raw_fd(),
                name.as_ptr(),
                AT_EMPTY_PATH,
            )
        };
        if linked != 0 {
            let error = std::io::Error::last_os_error();
            return if target.exists() {
                Err(FileError::staged("artifact-publish-target-exists"))
            } else {
                Err(FileError::io("artifact-publish-link", error))
            };
        }
    }
    #[cfg(not(target_os = "linux"))]
    {
        if let Err(error) = std::fs::hard_link(source, target) {
            return if target.exists() {
                Err(FileError::staged("artifact-publish-target-exists"))
            } else {
                Err(FileError::io("artifact-publish-link", error))
            };
        }
    }

    let opened = file
        .metadata()
        .map_err(|error| FileError::io("artifact-publish-opened-identity", error))?;
    let published = std::fs::symlink_metadata(target)
        .map_err(|error| FileError::io("artifact-publish-target-identity", error))?;
    let current_source = std::fs::symlink_metadata(source)
        .map_err(|error| FileError::io("artifact-publish-source-identity", error))?;
    if !same_inode(&opened, &published) || !same_inode(&opened, &current_source) {
        let _ = std::fs::remove_file(target);
        return Err(FileError::staged("artifact-publish-identity-mismatch"));
    }
    if let Err(error) = std::fs::remove_file(source) {
        let _ = std::fs::remove_file(target);
        return Err(FileError::io("artifact-publish-unlink", error));
    }
    Ok(())
}

fn same_inode(left: &std::fs::Metadata, right: &std::fs::Metadata) -> bool {
    left.dev() == right.dev() && left.ino() == right.ino()
}

fn remove(path: &Path) {
    let _ = std::fs::remove_file(path);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    struct FakeCleanup(bool);
    impl Cleanup for FakeCleanup {
        fn terminate_and_wait(&mut self) -> Result<(), Error> {
            if self.0 { Ok(()) } else { Err(Error::Resource) }
        }
    }

    fn root(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("hb-task-{label}-{}-{nonce}", std::process::id()));
        std::fs::create_dir(&path).unwrap();
        path
    }

    fn task() -> TaskId {
        TaskId::parse("task_one").unwrap()
    }

    fn file(value: &str) -> FileVersionId {
        FileVersionId::parse(value).unwrap()
    }

    fn binding<'a>(task: &'a TaskId, request: u8, epoch: u64) -> Binding<'a> {
        Binding {
            epoch,
            task,
            request: [request; 16],
        }
    }

    fn hash(bytes: &[u8]) -> [u8; 32] {
        let mut digest = Sha256::new();
        digest.update(bytes);
        digest.finish()
    }

    #[test]
    fn file_errors_preserve_stable_stage_and_raw_errno() {
        let io = FileError::io(
            "artifact-publish-link",
            std::io::Error::from_raw_os_error(18),
        );
        assert_eq!(io.stage(), "artifact-publish-link");
        assert_eq!(io.raw_os_error(), Some(18));

        let hash = FileError::staged("artifact-hash-mismatch");
        assert_eq!(hash.stage(), "artifact-hash-mismatch");
        assert_eq!(hash.raw_os_error(), None);
    }

    #[test]
    fn chunks_are_ordered_bounded_hashed_and_published_readonly() {
        let root = root("input");
        let task = task();
        let mut files = TaskFiles::open(&root, 7, task.clone(), 0, FileLimits::default()).unwrap();
        let id = file("file_input");
        files
            .begin_input(binding(&task, 1, 7), id.clone(), 6, hash(b"abcdef"))
            .unwrap();
        assert_eq!(
            files.input_chunk(binding(&task, 2, 7), &id, 1, b"bad"),
            Err(FileError::Order)
        );
        files
            .input_chunk(binding(&task, 3, 7), &id, 0, b"abc")
            .unwrap();
        assert_eq!(
            files.input_chunk(binding(&task, 4, 7), &id, 3, b"defg"),
            Err(FileError::Size)
        );
        files
            .input_chunk(binding(&task, 5, 7), &id, 3, b"def")
            .unwrap();
        let path = files.commit_input(binding(&task, 6, 7), &id).unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"abcdef");
        assert_eq!(
            std::fs::metadata(path).unwrap().permissions().mode() & 0o222,
            0
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn binding_replay_and_quotas_fail_closed() {
        let root = root("binding");
        let task = task();
        let limits = FileLimits {
            input_bytes: 3,
            input_total_bytes: 3,
            input_files: 1,
            ..FileLimits::default()
        };
        let mut files = TaskFiles::open(&root, 7, task.clone(), 0, limits).unwrap();
        let id = file("file_input");
        assert_eq!(
            files.begin_input(binding(&task, 1, 8), id.clone(), 3, hash(b"abc")),
            Err(FileError::Binding)
        );
        files
            .begin_input(binding(&task, 1, 7), id.clone(), 3, hash(b"abc"))
            .unwrap();
        assert_eq!(
            files.input_chunk(binding(&task, 1, 7), &id, 0, b"abc"),
            Err(FileError::Replay)
        );
        assert_eq!(
            files.begin_input(binding(&task, 2, 7), file("file_two"), 1, hash(b"x")),
            Err(FileError::Quota)
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn wrong_task_zero_request_and_oversized_chunk_fail_closed() {
        let root = root("negative-binding");
        let task = task();
        let other = TaskId::parse("task_other").unwrap();
        let id = file("file_input");
        let mut files = TaskFiles::open(&root, 7, task.clone(), 0, FileLimits::default()).unwrap();
        assert_eq!(
            files.begin_input(binding(&other, 1, 7), id.clone(), 1, hash(b"x")),
            Err(FileError::Binding)
        );
        assert_eq!(
            files.begin_input(
                Binding {
                    epoch: 7,
                    task: &task,
                    request: [0; 16]
                },
                id.clone(),
                1,
                hash(b"x")
            ),
            Err(FileError::Binding)
        );
        files
            .begin_input(binding(&task, 2, 7), id.clone(), 1, hash(b"x"))
            .unwrap();
        assert_eq!(
            files.input_chunk(binding(&task, 3, 7), &id, 0, &vec![0; FILE_CHUNK_BYTES + 1]),
            Err(FileError::Quota)
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn artifact_paths_hashes_and_media_types_are_verified() {
        use std::os::unix::fs::symlink;

        let root = root("artifact");
        let task = task();
        let mut files = TaskFiles::open(&root, 7, task.clone(), 0, FileLimits::default()).unwrap();
        std::fs::write(root.join("tmp/result"), b"a,b\n1,2\n").unwrap();
        let id = file("file_result");
        assert_eq!(
            files.commit_artifact(
                binding(&task, 1, 7),
                &id,
                "../result",
                "text/csv",
                8,
                hash(b"a,b\n1,2\n")
            ),
            Err(FileError::Path)
        );
        assert_eq!(
            files.commit_artifact(
                binding(&task, 2, 7),
                &id,
                "result",
                "application/octet-stream",
                8,
                hash(b"a,b\n1,2\n")
            ),
            Err(FileError::MediaType)
        );
        let path = files
            .commit_artifact(
                binding(&task, 3, 7),
                &id,
                "result",
                "text/csv",
                8,
                hash(b"a,b\n1,2\n"),
            )
            .unwrap();
        assert_eq!(path.extension().unwrap(), "csv");
        assert_eq!(
            files
                .commit_artifact(
                    binding(&task, 4, 7),
                    &id,
                    "result",
                    "text/csv",
                    8,
                    hash(b"a,b\n1,2\n")
                )
                .unwrap_err()
                .stage(),
            "artifact-target-exists"
        );
        symlink(&path, root.join("tmp/link")).unwrap();
        assert_eq!(
            files
                .commit_artifact(
                    binding(&task, 5, 7),
                    &file("file_link"),
                    "link",
                    "text/csv",
                    8,
                    hash(b"a,b\n1,2\n")
                )
                .unwrap_err()
                .stage(),
            "artifact-open"
        );
        std::fs::write(root.join("tmp/hard"), b"a,b\n1,2\n").unwrap();
        std::fs::hard_link(root.join("tmp/hard"), root.join("tmp/hard-alias")).unwrap();
        assert_eq!(
            files
                .commit_artifact(
                    binding(&task, 6, 7),
                    &file("file_hard"),
                    "hard",
                    "text/csv",
                    8,
                    hash(b"a,b\n1,2\n")
                )
                .unwrap_err()
                .stage(),
            "artifact-identity"
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn stop_disconnect_lease_and_restart_clean_without_replay() {
        let root = root("cleanup");
        let task = task();
        let id = file("file_input");
        let mut files = TaskFiles::open(&root, 7, task.clone(), 0, FileLimits::default()).unwrap();
        files
            .begin_input(binding(&task, 1, 7), id, 3, hash(b"abc"))
            .unwrap();
        std::fs::write(root.join("tmp/uncommitted"), b"x").unwrap();
        let mut failed = FakeCleanup(false);
        assert_eq!(files.tick(5_000, &mut failed), Err(FileError::Cleanup));
        assert_eq!(files.state(), Lifecycle::CleanupFailed);
        assert_eq!(std::fs::read_dir(root.join("incoming")).unwrap().count(), 0);
        assert_eq!(std::fs::read_dir(root.join("tmp")).unwrap().count(), 0);
        drop(files);

        let mut restarted =
            TaskFiles::open(&root, 8, task.clone(), 10_000, FileLimits::default()).unwrap();
        assert_eq!(std::fs::read_dir(root.join("incoming")).unwrap().count(), 0);
        assert_eq!(std::fs::read_dir(root.join("tmp")).unwrap().count(), 0);
        assert_eq!(
            restarted.heartbeat(binding(&task, 2, 7), 1, 10_001),
            Err(FileError::Binding)
        );
        let mut cleaned = FakeCleanup(true);
        restarted.disconnect(&mut cleaned).unwrap();
        assert_eq!(restarted.state(), Lifecycle::Interrupted);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn cleanup_removes_symlinks_without_following_them() {
        use std::os::unix::fs::symlink;

        let root = root("cleanup-symlink");
        let task = task();
        let mut files = TaskFiles::open(&root, 7, task.clone(), 0, FileLimits::default()).unwrap();
        symlink(root.join("input"), root.join("tmp/escape")).unwrap();
        let mut cleaned = FakeCleanup(true);
        files.stop(binding(&task, 1, 7), &mut cleaned).unwrap();
        assert_eq!(files.state(), Lifecycle::Stopped);
        assert!(!root.join("tmp/escape").exists());
        assert!(root.join("input").is_dir());
        std::fs::remove_dir_all(root).unwrap();
    }
}
