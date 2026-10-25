//! Candidate supervisor policy. Production task admission stays closed until Linux integration.
#[cfg(all(target_os = "linux", not(target_arch = "aarch64")))]
compile_error!("hb-supervisor supports Linux only with the AArch64 open(2) ABI");

mod aarch64_linux_open_flags;
pub mod file_runtime;
pub(crate) mod policy;
pub mod process;
pub(crate) mod quota;
pub mod runtime;
pub mod sandbox;
pub(crate) mod seccomp;
pub mod task_channel;

#[cfg(any(target_os = "linux", target_os = "macos"))]
pub(crate) mod virtio_channel {
    use super::Error;
    use std::fs::{File, OpenOptions};
    use std::os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt};
    use std::path::PathBuf;
    use std::time::Duration;

    pub(crate) struct Roots {
        links: PathBuf,
        devices: PathBuf,
        sysfs: PathBuf,
        target_prefix: &'static str,
    }

    impl Roots {
        pub(crate) fn system() -> Self {
            Self {
                links: PathBuf::from("/dev/virtio-ports"),
                devices: PathBuf::from("/dev"),
                sysfs: PathBuf::from("/sys/class/virtio-ports"),
                target_prefix: "vport",
            }
        }

        #[cfg(test)]
        fn fixture(
            links: &std::path::Path,
            devices: &std::path::Path,
            sysfs: &std::path::Path,
            target_prefix: &'static str,
        ) -> Self {
            Self {
                links: links.to_path_buf(),
                devices: devices.to_path_buf(),
                sysfs: sysfs.to_path_buf(),
                target_prefix,
            }
        }
    }

    pub(crate) struct Channels {
        pub(crate) control: File,
        pub(crate) file: File,
        pub(crate) inference: File,
    }

    pub(crate) fn open_fixed_channels() -> Result<Channels, Error> {
        open_fixed_channels_with_pause(
            &Roots::system(),
            41,
            Duration::from_millis(25),
            std::thread::sleep,
        )
    }

    fn open_fixed_channels_with_pause<F>(
        roots: &Roots,
        attempts: usize,
        delay: Duration,
        mut pause: F,
    ) -> Result<Channels, Error>
    where
        F: FnMut(Duration),
    {
        let mut last_error = Error::Boundary;
        for attempt in 0..attempts {
            let opened = (|| {
                Ok(Channels {
                    control: open_channel_at_with_hook(roots, "hb.control", || {})?,
                    file: open_channel_at_with_hook(roots, "hb.file", || {})?,
                    inference: open_channel_at_with_hook(roots, "hb.inference", || {})?,
                })
            })();
            match opened {
                Ok(channels) => return Ok(channels),
                Err(error) => last_error = error,
            }
            if attempt + 1 < attempts {
                pause(delay);
            }
        }
        Err(last_error)
    }

    fn open_channel_at_with_hook<F>(
        roots: &Roots,
        expected_name: &str,
        before_open: F,
    ) -> Result<File, Error>
    where
        F: FnOnce(),
    {
        let link_path = roots.links.join(expected_name);
        let link_before = std::fs::symlink_metadata(&link_path).map_err(crate::io)?;
        if !link_before.file_type().is_symlink() {
            return Err(Error::Boundary);
        }
        let target = std::fs::read_link(&link_path).map_err(crate::io)?;
        let target_name = target
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or(Error::Boundary)?;
        if !target.is_absolute()
            || target.parent() != Some(roots.devices.as_path())
            || !target_name.starts_with(roots.target_prefix)
        {
            return Err(Error::Boundary);
        }

        let port = roots.sysfs.join(target_name);
        let registered_name = read_sysfs_value(&port.join("name"))?;
        let registered_device = read_sysfs_value(&port.join("dev"))?;
        let expected_device = parse_device(&registered_device)?;
        if registered_name != expected_name {
            return Err(Error::Boundary);
        }

        let target_before = std::fs::symlink_metadata(&target).map_err(crate::io)?;
        if target_before.file_type().is_symlink() || !target_before.file_type().is_char_device() {
            return Err(Error::Boundary);
        }
        before_open();
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(no_follow_flag())
            .open(&target)
            .map_err(crate::io)?;
        let opened = file.metadata().map_err(crate::io)?;
        if !opened.file_type().is_char_device()
            || device_numbers(opened.rdev()) != expected_device
            || (opened.dev(), opened.ino(), opened.rdev())
                != (
                    target_before.dev(),
                    target_before.ino(),
                    target_before.rdev(),
                )
        {
            return Err(Error::Boundary);
        }

        let link_after = std::fs::symlink_metadata(&link_path).map_err(crate::io)?;
        let target_after = std::fs::symlink_metadata(&target).map_err(crate::io)?;
        if !link_after.file_type().is_symlink()
            || (link_after.dev(), link_after.ino()) != (link_before.dev(), link_before.ino())
            || std::fs::read_link(&link_path).map_err(crate::io)? != target
            || (target_after.dev(), target_after.ino(), target_after.rdev())
                != (opened.dev(), opened.ino(), opened.rdev())
            || !target_after.file_type().is_char_device()
            || read_sysfs_value(&port.join("name"))? != registered_name
            || read_sysfs_value(&port.join("dev"))? != registered_device
        {
            return Err(Error::Boundary);
        }
        Ok(file)
    }

    fn read_sysfs_value(path: &std::path::Path) -> Result<String, Error> {
        let value = std::fs::read_to_string(path).map_err(crate::io)?;
        let value = value.strip_suffix('\n').unwrap_or(&value);
        if value.is_empty() || value.contains(['\n', '\r']) {
            return Err(Error::Boundary);
        }
        Ok(value.to_owned())
    }

    fn parse_device(value: &str) -> Result<(u64, u64), Error> {
        let (major, minor) = value.split_once(':').ok_or(Error::Boundary)?;
        let major = parse_decimal(major)?;
        let minor = parse_decimal(minor)?;
        Ok((major, minor))
    }

    fn parse_decimal(value: &str) -> Result<u64, Error> {
        let parsed = value.parse::<u64>().map_err(|_| Error::Boundary)?;
        if parsed.to_string() != value {
            return Err(Error::Boundary);
        }
        Ok(parsed)
    }

    #[cfg(target_os = "linux")]
    const fn no_follow_flag() -> i32 {
        crate::aarch64_linux_open_flags::O_NOFOLLOW
    }

    #[cfg(target_os = "macos")]
    const fn no_follow_flag() -> i32 {
        0x0000_0100
    }

    #[cfg(target_os = "linux")]
    fn device_numbers(device: u64) -> (u64, u64) {
        (
            ((device >> 8) & 0xfff) | ((device >> 32) & 0xffff_f000),
            (device & 0xff) | ((device >> 12) & 0xffff_ff00),
        )
    }

    #[cfg(target_os = "macos")]
    fn device_numbers(device: u64) -> (u64, u64) {
        ((device >> 24) & 0xff, device & 0x00ff_ffff)
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::fs;
        use std::os::unix::fs::{FileTypeExt, MetadataExt, symlink};
        use std::path::Path;
        use std::sync::atomic::{AtomicU64, Ordering};

        static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(0);
        const PORTS: [(&str, &str); 3] = [
            ("hb.control", "null"),
            ("hb.file", "zero"),
            ("hb.inference", "random"),
        ];

        struct Fixture {
            root: PathBuf,
            links: PathBuf,
            sysfs: PathBuf,
        }

        impl Fixture {
            fn new() -> Self {
                let root = std::env::temp_dir().join(format!(
                    "havenbox-virtio-channel-{}-{}",
                    std::process::id(),
                    NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
                ));
                let links = root.join("links");
                let sysfs = root.join("sysfs");
                fs::create_dir_all(&links).unwrap();
                fs::create_dir_all(&sysfs).unwrap();
                let fixture = Self { root, links, sysfs };
                for (name, node) in PORTS {
                    fixture.publish(name, node, name, None);
                }
                fixture
            }

            fn roots(&self) -> Roots {
                Roots::fixture(&self.links, Path::new("/dev"), &self.sysfs, "")
            }

            fn publish(&self, name: &str, node: &str, registered_name: &str, dev: Option<&str>) {
                let link = self.links.join(name);
                let _ = fs::remove_file(&link);
                symlink(Path::new("/dev").join(node), link).unwrap();
                let port = self.sysfs.join(node);
                fs::create_dir_all(&port).unwrap();
                fs::write(port.join("name"), format!("{registered_name}\n")).unwrap();
                let metadata = fs::metadata(Path::new("/dev").join(node)).unwrap();
                let (major, minor) = device_numbers(metadata.rdev());
                let registered_dev = dev
                    .map(str::to_owned)
                    .unwrap_or_else(|| format!("{major}:{minor}"));
                fs::write(port.join("dev"), format!("{registered_dev}\n")).unwrap();
            }
        }

        impl Drop for Fixture {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.root);
            }
        }

        #[test]
        fn waits_until_all_three_opened_devices_match_name_and_dev() {
            let fixture = Fixture::new();
            fixture.publish("hb.inference", "random", "hb.inference", Some("999:999"));
            let mut pauses = 0;
            let channels =
                open_fixed_channels_with_pause(&fixture.roots(), 2, Duration::ZERO, |_| {
                    pauses += 1;
                    fixture.publish("hb.inference", "random", "hb.inference", None);
                })
                .unwrap();
            assert_eq!(pauses, 1);
            for file in [channels.control, channels.file, channels.inference] {
                assert!(file.metadata().unwrap().file_type().is_char_device());
            }
        }

        #[test]
        fn rejects_wrong_name_wrong_dev_non_character_and_link_replacement() {
            #[cfg(target_os = "linux")]
            assert_eq!(device_numbers((253 << 8) | 17), (253, 17));
            assert_eq!(parse_device("253:17"), Ok((253, 17)));
            assert_eq!(parse_device("253"), Err(Error::Boundary));
            assert_eq!(parse_device("0253:17"), Err(Error::Boundary));
            let fixture = Fixture::new();
            fixture.publish("hb.control", "null", "hb.wrong", None);
            assert!(open_channel_at_with_hook(&fixture.roots(), "hb.control", || {}).is_err());

            fixture.publish("hb.control", "null", "hb.control", Some("999:999"));
            assert!(open_channel_at_with_hook(&fixture.roots(), "hb.control", || {}).is_err());

            fixture.publish("hb.control", "null", "hb.control", None);
            let link = fixture.links.join("hb.control");
            assert!(
                open_channel_at_with_hook(&fixture.roots(), "hb.control", || {
                    fs::remove_file(&link).unwrap();
                    symlink("/dev/zero", &link).unwrap();
                })
                .is_err()
            );

            let devices = fixture.root.join("devices");
            fs::create_dir_all(&devices).unwrap();
            fs::write(devices.join("vport0p1"), b"not a device").unwrap();
            let roots = Roots::fixture(&fixture.links, &devices, &fixture.sysfs, "vport");
            let link = fixture.links.join("hb.control");
            fs::remove_file(&link).unwrap();
            symlink(devices.join("vport0p1"), link).unwrap();
            fs::create_dir_all(fixture.sysfs.join("vport0p1")).unwrap();
            fs::write(fixture.sysfs.join("vport0p1/name"), "hb.control\n").unwrap();
            fs::write(fixture.sysfs.join("vport0p1/dev"), "1:3\n").unwrap();
            assert!(open_channel_at_with_hook(&roots, "hb.control", || {}).is_err());
        }
    }
}
use std::collections::HashSet;
use std::path::{Path, PathBuf};

#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    Lease,
    Handshake,
    Replay,
    Boundary,
    Resource,
    Unsupported,
    Io(String),
    Protocol,
}
#[derive(Debug, PartialEq, Eq)]
pub enum State {
    AwaitingHello,
    Ready,
    Interrupted,
}

pub struct Supervisor {
    state: State,
    instance: [u8; 16],
    epoch: u64,
    deadline_ms: u64,
    last_ms: u64,
    sequence: u64,
    seen: HashSet<[u8; 16]>,
    active: bool,
    completion_reported: bool,
}
impl Supervisor {
    pub fn new(instance: [u8; 16], epoch: u64, now_ms: u64) -> Result<Self, Error> {
        if instance == [0; 16] || epoch == 0 {
            return Err(Error::Handshake);
        }
        Ok(Self {
            state: State::AwaitingHello,
            instance,
            epoch,
            deadline_ms: now_ms.checked_add(5_000).ok_or(Error::Lease)?,
            last_ms: now_ms,
            sequence: 0,
            seen: HashSet::new(),
            active: false,
            completion_reported: false,
        })
    }
    /// State is observable but cannot be replaced by callers.
    /// ```compile_fail
    /// use hb_supervisor::{Supervisor, State};
    /// let mut s = Supervisor::new([1; 16], 1, 0).unwrap();
    /// s.state = State::Ready;
    /// ```
    pub fn state(&self) -> &State {
        &self.state
    }
    pub fn tick(&mut self, now_ms: u64) -> Result<(), Error> {
        if self.state == State::Interrupted || now_ms < self.last_ms || now_ms >= self.deadline_ms {
            self.interrupt();
            return Err(Error::Lease);
        }
        self.last_ms = now_ms;
        Ok(())
    }
    pub fn hello(&mut self, instance: [u8; 16], epoch: u64, now_ms: u64) -> Result<(), Error> {
        self.tick(now_ms)?;
        if self.state != State::AwaitingHello || instance != self.instance || epoch != self.epoch {
            self.interrupt();
            return Err(Error::Handshake);
        }
        self.state = State::Ready;
        Ok(())
    }
    pub fn renew(&mut self, sequence: u64, now_ms: u64) -> Result<(), Error> {
        self.tick(now_ms)?;
        if self.state != State::Ready || sequence <= self.sequence {
            self.interrupt();
            return Err(Error::Replay);
        }
        self.deadline_ms = now_ms.checked_add(5_000).ok_or_else(|| {
            self.interrupt();
            Error::Lease
        })?;
        self.sequence = sequence;
        Ok(())
    }
    /// One active synthetic boundary check; not a CallPermit or product authorization.
    pub fn admit_probe(&mut self, request: [u8; 16], now_ms: u64) -> Result<(), Error> {
        self.tick(now_ms)?;
        if self.state != State::Ready
            || self.active
            || request == [0; 16]
            || self.seen.len() >= 4096
        {
            return Err(Error::Boundary);
        }
        if !self.seen.insert(request) {
            return Err(Error::Replay);
        }
        self.active = true;
        Ok(())
    }
    /// Completion is not cleanup evidence. The reservation remains held.
    pub fn finish_probe(&mut self) {
        if self.active {
            self.completion_reported = true;
        }
    }
    /// No caller-supplied boolean/token can attest cleanup. Until admission owns a
    /// real cgroup and its process identities, even an empty fixed cgroup is not
    /// proof about this reservation. Deliberately no slot-release implementation.
    pub fn confirm_cleanup(&mut self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Production execution remains closed, independently of synthetic probes.
    pub fn admit_execution(&self) -> Result<(), Error> {
        Err(Error::Unsupported)
    }
    /// Interruption is terminal and retains any reservation.
    pub fn interrupt(&mut self) {
        self.state = State::Interrupted;
    }
    pub fn cleanup_required(&self) -> bool {
        self.active && (self.state == State::Interrupted || self.completion_reported)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    Harness,
    Worker,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SyntheticCase {
    Normal,
    OutputOverflow,
    Hold,
}
/// Exact topology, not a list of requested arbitrary bind mounts.
#[derive(Debug, PartialEq, Eq)]
pub struct Boundary {
    pub role: Role,
    pub uid: u32,
    pub input_readonly: bool,
    pub visible: Vec<String>,
    pub memory_bytes: u64,
    pub pids: u32,
    pub scratch_bytes: u64,
}
impl Boundary {
    pub fn validate(&self) -> Result<(), Error> {
        let profile = crate::policy::SyntheticProfile::for_role(self.role);
        let expected = profile.boundary();
        if self.uid != expected.uid
            || self.input_readonly != expected.input_readonly
            || self.visible != expected.visible
        {
            return Err(Error::Boundary);
        }
        if self.memory_bytes != expected.memory_bytes
            || self.pids != expected.pids
            || self.scratch_bytes != expected.scratch_bytes
        {
            return Err(Error::Resource);
        }
        Ok(())
    }
}

/// Only the fixed supervisor-owned cgroup may be addressed; never accept a wire path.
pub struct TaskCgroup {
    path: PathBuf,
}
impl TaskCgroup {
    pub fn fixed() -> Self {
        Self {
            path: PathBuf::from("/sys/fs/cgroup/havenbox/active"),
        }
    }
    fn write(&self, name: &str, value: &str) -> Result<(), Error> {
        if !cfg!(target_os = "linux") {
            return Err(Error::Unsupported);
        }
        let file = self.path.join(name);
        if std::fs::symlink_metadata(&file)
            .map_err(io)?
            .file_type()
            .is_symlink()
        {
            return Err(Error::Boundary);
        }
        std::fs::write(file, value).map_err(io)
    }
    fn write_verified(&self, name: &str, value: &str) -> Result<(), Error> {
        self.write(name, value)?;
        let applied = std::fs::read_to_string(self.path.join(name)).map_err(io)?;
        if applied.trim_end() == value {
            Ok(())
        } else {
            Err(Error::Resource)
        }
    }
    pub fn configure(&self, boundary: &Boundary) -> Result<(), Error> {
        boundary.validate()?;
        if !cfg!(target_os = "linux") {
            return Err(Error::Unsupported);
        }
        if self.path.canonicalize().map_err(io)? != self.path {
            return Err(Error::Boundary);
        }
        self.write_verified("memory.max", &boundary.memory_bytes.to_string())?;
        self.write_verified("memory.swap.max", "0")?;
        self.write_verified("memory.oom.group", "1")?;
        self.write_verified("pids.max", &boundary.pids.to_string())?;
        self.write_verified("cpu.max", "100000 100000")
    }
    pub fn admit(&self, pid: u32) -> Result<(), Error> {
        if pid <= 1 {
            return Err(Error::Boundary);
        }
        self.write("cgroup.procs", &pid.to_string())
    }
    pub fn kill(&self) -> Result<(), Error> {
        self.write("cgroup.kill", "1")
    }
    pub fn empty(&self) -> Result<bool, Error> {
        if !cfg!(target_os = "linux") {
            return Err(Error::Unsupported);
        }
        let events = std::fs::read_to_string(self.path.join("cgroup.events")).map_err(io)?;
        if events.lines().any(|l| l == "populated 0") {
            Ok(true)
        } else if events.lines().any(|l| l == "populated 1") {
            Ok(false)
        } else {
            Err(Error::Boundary)
        }
    }
    pub fn kill_and_wait_empty(&self, timeout: std::time::Duration) -> Result<(), Error> {
        self.kill()?;
        let deadline = std::time::Instant::now() + timeout;
        loop {
            if self.empty()? {
                return Ok(());
            }
            if std::time::Instant::now() >= deadline {
                return Err(Error::Resource);
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }
}
fn io(error: std::io::Error) -> Error {
    Error::Io(error.to_string())
}

/// Candidate image topology, not an inferred allowlist. All entries are required;
/// unknown mounts, optional propagation fields and alternate spellings fail closed.
/// This profile still needs validation against the eventual immutable Linux image.
const BOOT_MOUNTS: &[(&str, &str, &str, &str, &str)] = &[
    ("/", "ext4", "/dev/vda", "ro,relatime", "ro"),
    (
        "/data",
        "ext4",
        "/dev/vdb",
        "rw,nosuid,nodev,noexec,relatime",
        "rw,prjquota",
    ),
    (
        "/proc",
        "proc",
        "proc",
        "rw,nosuid,nodev,noexec,relatime",
        "rw",
    ),
    (
        "/sys",
        "sysfs",
        "sysfs",
        "ro,nosuid,nodev,noexec,relatime",
        "ro",
    ),
    (
        "/dev",
        "devtmpfs",
        "devtmpfs",
        "rw,nosuid,noexec,relatime",
        "rw,size=65536k,nr_inodes=16384,mode=755,inode64",
    ),
    (
        "/dev/pts",
        "devpts",
        "devpts",
        "rw,nosuid,noexec,relatime",
        "rw,gid=5,mode=620,ptmxmode=000",
    ),
    (
        "/sys/fs/cgroup",
        "cgroup2",
        "cgroup",
        "rw,nosuid,nodev,noexec,relatime",
        "rw",
    ),
    (
        "/tmp",
        "tmpfs",
        "tmpfs",
        "rw,nosuid,nodev,noexec,relatime",
        "rw,size=65536k,inode64",
    ),
];

fn decimal(value: &str) -> Result<u32, Error> {
    let number: u32 = value.parse().map_err(|_| Error::Boundary)?;
    if number.to_string() != value {
        return Err(Error::Boundary);
    }
    Ok(number)
}
fn device(value: &str) -> Result<(u32, u32), Error> {
    let (major, minor) = value.split_once(':').ok_or(Error::Boundary)?;
    Ok((decimal(major)?, decimal(minor)?))
}

/// block_devices is the complete /sys/class/block name + dev inventory, not
/// merely the mounted disks; unmounted disks and partitions also reject boot.
pub fn inspect_boot(
    mountinfo: &str,
    network_names: &[String],
    block_devices: &[(String, String)],
) -> Result<(), Error> {
    if network_names != ["lo"]
        || block_devices
            != [
                ("vda".into(), "253:0".into()),
                ("vdb".into(), "253:16".into()),
            ]
    {
        return Err(Error::Boundary);
    }
    let mut mounts = std::collections::HashMap::new();
    let mut ids = HashSet::new();
    let mut devices = HashSet::new();
    for line in mountinfo.lines() {
        let (left, right) = line.split_once(" - ").ok_or(Error::Boundary)?;
        let fields: Vec<_> = left.split(' ').collect();
        let fs: Vec<_> = right.split(' ').collect();
        if fields.len() != 6 || fs.len() != 3 || fields[3] != "/" {
            return Err(Error::Boundary);
        }
        let id = decimal(fields[0])?;
        let parent = decimal(fields[1])?;
        let dev = device(fields[2])?;
        if id == 0 || id == parent || !ids.insert(id) || !devices.insert(dev) {
            return Err(Error::Boundary);
        }
        let profile = BOOT_MOUNTS
            .iter()
            .find(|p| p.0 == fields[4])
            .ok_or(Error::Boundary)?;
        if (fs[0], fs[1], fields[5], fs[2]) != (profile.1, profile.2, profile.3, profile.4) {
            return Err(Error::Boundary);
        }
        let valid_device = match fields[4] {
            "/" => dev == (253, 0),
            "/data" => dev == (253, 16),
            _ => dev.0 == 0 && dev.1 != 0,
        };
        if !valid_device || mounts.insert(fields[4], (id, parent)).is_some() {
            return Err(Error::Boundary);
        }
    }
    if mounts.len() != BOOT_MOUNTS.len() {
        return Err(Error::Boundary);
    }
    for (path, &(id, parent)) in &mounts {
        let expected_parent = match *path {
            "/" => {
                if ids.contains(&parent) {
                    return Err(Error::Boundary);
                }
                continue;
            }
            "/dev/pts" => "/dev",
            "/sys/fs/cgroup" => "/sys",
            _ => "/",
        };
        if parent != mounts[expected_parent].0 || id == parent {
            return Err(Error::Boundary);
        }
    }
    Ok(())
}

pub fn inspect_live_boot() -> Result<(), Error> {
    if !cfg!(target_os = "linux") {
        return Err(Error::Unsupported);
    }
    let mounts = std::fs::read_to_string("/proc/self/mountinfo").map_err(io)?;
    let mut network = Vec::new();
    for entry in std::fs::read_dir(Path::new("/sys/class/net")).map_err(io)? {
        network.push(
            entry
                .map_err(io)?
                .file_name()
                .into_string()
                .map_err(|_| Error::Boundary)?,
        );
    }
    network.sort();
    let mut blocks = Vec::new();
    for entry in std::fs::read_dir("/sys/class/block").map_err(io)? {
        let entry = entry.map_err(io)?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| Error::Boundary)?;
        let dev = std::fs::read_to_string(entry.path().join("dev")).map_err(io)?;
        let dev = dev.strip_suffix('\n').ok_or(Error::Boundary)?.to_string();
        device(&dev)?;
        blocks.push((name, dev));
    }
    blocks.sort();
    inspect_boot(&mounts, &network, &blocks)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lease_is_terminal_and_does_not_replay() {
        let mut s = Supervisor::new([1; 16], 7, 0).unwrap();
        s.hello([1; 16], 7, 1).unwrap();
        s.admit_probe([2; 16], 2).unwrap();
        assert_eq!(s.tick(5000), Err(Error::Lease));
        assert!(s.cleanup_required());
        assert_eq!(s.renew(1, 5001), Err(Error::Lease));
        assert_eq!(s.hello([1; 16], 7, 5002), Err(Error::Lease));
        s.finish_probe();
        assert_eq!(s.confirm_cleanup(), Err(Error::Unsupported));
        assert!(s.cleanup_required());
        assert_eq!(s.admit_probe([3; 16], 2), Err(Error::Lease));
        assert_eq!(s.admit_probe([3; 16], 5003), Err(Error::Lease));
        assert_eq!(s.state(), &State::Interrupted);
    }
    #[test]
    fn handshake_sequence_and_requests_are_bound() {
        let mut s = Supervisor::new([1; 16], 7, 0).unwrap();
        assert_eq!(s.hello([2; 16], 7, 1), Err(Error::Handshake));
        let mut s = Supervisor::new([1; 16], 7, 0).unwrap();
        s.hello([1; 16], 7, 1).unwrap();
        s.renew(1, 2).unwrap();
        s.admit_probe([2; 16], 3).unwrap();
        s.finish_probe();
        assert!(s.cleanup_required());
        assert_eq!(s.confirm_cleanup(), Err(Error::Unsupported));
        assert_eq!(s.admit_probe([3; 16], 4), Err(Error::Boundary));
        assert_eq!(s.admit_execution(), Err(Error::Unsupported));
        assert_eq!(s.renew(1, 4), Err(Error::Replay));
        assert_eq!(s.state(), &State::Interrupted);
    }
    #[test]
    fn harness_never_maps_input_other_sessions_or_worker_tmp() {
        let mut b = Boundary {
            role: Role::Harness,
            uid: 10001,
            input_readonly: true,
            visible: vec!["/context".into(), "/history".into(), "/tmp".into()],
            memory_bytes: 536870912,
            pids: 64,
            scratch_bytes: 2147483648,
        };
        b.validate().unwrap();
        for forbidden in ["/input", "/data", "/sessions", "/worker/tmp", "/dev"] {
            b.visible.push(forbidden.into());
            assert_eq!(b.validate(), Err(Error::Boundary));
            b.visible.pop();
        }
        b.memory_bytes += 1;
        assert_eq!(b.validate(), Err(Error::Resource));
    }
    #[test]
    fn boot_drift_rejected() {
        let mounts = BOOT_MOUNTS
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let dev = match i {
                    0 => "253:0".into(),
                    1 => "253:16".into(),
                    _ => format!("0:{}", i + 1),
                };
                let parent = match p.0 {
                    "/" => 0,
                    "/dev/pts" => 5,
                    "/sys/fs/cgroup" => 4,
                    _ => 1,
                };
                format!(
                    "{} {parent} {dev} / {} {} - {} {} {}",
                    i + 1,
                    p.0,
                    p.3,
                    p.1,
                    p.2,
                    p.4
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        let blocks = vec![
            ("vda".into(), "253:0".into()),
            ("vdb".into(), "253:16".into()),
        ];
        let lo = vec!["lo".into()];
        inspect_boot(&mounts, &lo, &blocks).unwrap();
        for extra in [
            "9 1 0:9 / /context rw - tmpfs tmpfs rw",
            "9 1 0:9 / /share rw - fuse.sshfs host rw",
            "9 1 254:0 / / ro,relatime - ext4 /dev/vda ro",
            "9 1 0:9 / /share rw - virtiofs host rw",
        ] {
            assert!(inspect_boot(&format!("{mounts}\n{extra}"), &lo, &blocks).is_err());
        }
        for (from, to) in [
            ("253:0 / /", "253:0 /subtree /"),
            ("253:16", "253:17"),
            ("253:0", "252:0"),
            ("ro,relatime", "ro,rw,relatime"),
            ("ro,relatime", "ro,relatime shared:1"),
            ("1 0 ", "1 2 "),
            ("2 1 ", "1 1 "),
            (" /data ", " /data\\040 "),
            (" - ext4", " - ext4 extra"),
        ] {
            assert!(
                inspect_boot(&mounts.replace(from, to), &lo, &blocks).is_err(),
                "{from} -> {to}"
            );
        }
        assert!(
            inspect_boot(
                &mounts.lines().skip(1).collect::<Vec<_>>().join("\n"),
                &lo,
                &blocks
            )
            .is_err()
        );
        assert!(inspect_boot(&mounts, &["lo".into(), "eth0".into()], &blocks).is_err());
        assert!(inspect_boot(&mounts, &lo, &blocks[..1]).is_err());
        let mut extra = blocks.clone();
        extra.push(("vdc".into(), "254:32".into()));
        assert!(inspect_boot(&mounts, &lo, &extra).is_err());
    }
    #[test]
    fn boot_accepts_only_the_frozen_devtmpfs_capacity() {
        let mounts = concat!(
            "1 0 253:0 / / ro,relatime - ext4 /dev/vda ro\n",
            "2 1 253:16 / /data rw,nosuid,nodev,noexec,relatime - ext4 /dev/vdb rw,prjquota\n",
            "3 1 0:3 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw\n",
            "4 1 0:4 / /sys ro,nosuid,nodev,noexec,relatime - sysfs sysfs ro\n",
            "5 1 0:5 / /dev rw,nosuid,noexec,relatime - devtmpfs devtmpfs rw,size=65536k,nr_inodes=16384,mode=755,inode64\n",
            "6 5 0:6 / /dev/pts rw,nosuid,noexec,relatime - devpts devpts rw,gid=5,mode=620,ptmxmode=000\n",
            "7 4 0:7 / /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw\n",
            "8 1 0:8 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=65536k,inode64"
        );
        let blocks = vec![
            ("vda".into(), "253:0".into()),
            ("vdb".into(), "253:16".into()),
        ];
        let lo = vec!["lo".into()];

        inspect_boot(mounts, &lo, &blocks).unwrap();
        for drifted in [
            mounts.replace(
                "devtmpfs devtmpfs rw,size=65536k",
                "devtmpfs devtmpfs rw,size=65535k",
            ),
            mounts.replace(
                "devtmpfs devtmpfs rw,size=65536k",
                "devtmpfs devtmpfs rw,size=65537k",
            ),
            mounts.replace("nr_inodes=16384", "nr_inodes=16383"),
            mounts.replace("nr_inodes=16384", "nr_inodes=16385"),
            mounts.replace("mode=755,inode64", "mode=755,inode64,unexpected"),
        ] {
            assert_eq!(inspect_boot(&drifted, &lo, &blocks), Err(Error::Boundary));
        }
    }
    #[test]
    fn unsupported_host_does_not_claim_isolation() {
        if !cfg!(target_os = "linux") {
            assert_eq!(inspect_live_boot(), Err(Error::Unsupported));
            assert_eq!(TaskCgroup::fixed().kill(), Err(Error::Unsupported));
        }
    }
}
