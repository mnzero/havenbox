//! Candidate supervisor policy. Production task admission stays closed until Linux integration.
pub mod sandbox;
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

#[derive(Debug, PartialEq, Eq)]
pub enum Role {
    Harness,
    Worker,
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
        let (uid, paths): (u32, &[&str]) = match self.role {
            Role::Harness => (10001, &["/context", "/history", "/tmp"]),
            Role::Worker => (10002, &["/input", "/output", "/tmp"]),
        };
        if self.uid != uid || !self.input_readonly || self.visible != paths {
            return Err(Error::Boundary);
        }
        if self.memory_bytes == 0
            || self.memory_bytes > 536_870_912
            || self.pids == 0
            || self.pids > 64
            || self.scratch_bytes == 0
            || self.scratch_bytes > 2_147_483_648
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
    pub fn configure(&self, boundary: &Boundary) -> Result<(), Error> {
        boundary.validate()?;
        if !cfg!(target_os = "linux") {
            return Err(Error::Unsupported);
        }
        if self.path.canonicalize().map_err(io)? != self.path {
            return Err(Error::Boundary);
        }
        self.write("memory.max", &boundary.memory_bytes.to_string())?;
        self.write("memory.swap.max", "0")?;
        self.write("memory.oom.group", "1")?;
        self.write("pids.max", &boundary.pids.to_string())?;
        self.write("cpu.max", "100000 100000")
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
        "rw",
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
        "rw,nosuid,noexec",
        "rw,size=65536k,nr_inodes=16384,mode=755",
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
        "rw,nosuid,nodev,noexec",
        "rw,size=65536k",
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
                ("vda".into(), "254:0".into()),
                ("vdb".into(), "254:16".into()),
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
            "/" => dev == (254, 0),
            "/data" => dev == (254, 16),
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
                    0 => "254:0".into(),
                    1 => "254:16".into(),
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
            ("vda".into(), "254:0".into()),
            ("vdb".into(), "254:16".into()),
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
            ("254:0 / /", "254:0 /subtree /"),
            ("254:16", "254:17"),
            ("254:0", "253:0"),
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
    fn unsupported_host_does_not_claim_isolation() {
        if !cfg!(target_os = "linux") {
            assert_eq!(inspect_live_boot(), Err(Error::Unsupported));
            assert_eq!(TaskCgroup::fixed().kill(), Err(Error::Unsupported));
        }
    }
}
