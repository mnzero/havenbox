//! Linux-only namespace construction, callable only in a fresh single-threaded child.
//! Only the fixed HB-11 synthetic path calls this; product admission remains closed.
//! A syscall failure must terminate that child.
use crate::{Boundary, Error};

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use crate::Role;
    use std::ffi::{CString, c_char, c_int, c_ulong, c_void};
    use std::os::unix::fs::MetadataExt;
    unsafe extern "C" {
        fn unshare(flags: c_int) -> c_int;
        fn mount(
            source: *const c_char,
            target: *const c_char,
            kind: *const c_char,
            flags: c_ulong,
            data: *const c_void,
        ) -> c_int;
        fn chroot(path: *const c_char) -> c_int;
        fn chdir(path: *const c_char) -> c_int;
        fn setgroups(size: usize, groups: *const u32) -> c_int;
        fn setgid(gid: u32) -> c_int;
        fn setuid(uid: u32) -> c_int;
        fn geteuid() -> u32;
        fn prctl(option: c_int, ...) -> c_int;
        fn setrlimit(resource: c_int, limit: *const Limit) -> c_int;
    }
    #[repr(C)]
    struct Limit {
        current: u64,
        maximum: u64,
    }
    fn checked(result: c_int) -> Result<(), Error> {
        if result == 0 {
            Ok(())
        } else {
            Err(Error::Io(std::io::Error::last_os_error().to_string()))
        }
    }
    fn c(text: &str) -> CString {
        CString::new(text).expect("fixed internal mount string")
    }
    fn mount_at(
        source: &str,
        target: &str,
        kind: &str,
        flags: c_ulong,
        options: &str,
    ) -> Result<(), Error> {
        let (source, target, kind, options) = (c(source), c(target), c(kind), c(options));
        checked(unsafe {
            mount(
                source.as_ptr(),
                target.as_ptr(),
                kind.as_ptr(),
                flags,
                options.as_ptr().cast(),
            )
        })
    }
    fn bind(source: &str, target: &str, readonly: bool, owner: u32) -> Result<(), Error> {
        validate_source(source, readonly, owner)?;
        mount_at(source, target, "", 4096, "")?;
        // Remount with nosuid,nodev; noexec for task data, including writable outputs.
        mount_at(
            "",
            target,
            "",
            4096 | 32 | 2 | 4 | 8 | u64::from(readonly),
            "",
        )
    }
    fn validate_source(source: &str, readonly: bool, owner: u32) -> Result<(), Error> {
        let path = std::path::Path::new(source);
        if !path.is_absolute() || path.canonicalize().map_err(crate::io)? != path {
            return Err(Error::Boundary);
        }
        let mut current = std::path::PathBuf::from("/");
        let components: Vec<_> = path.components().skip(1).collect();
        for (index, component) in components.iter().enumerate() {
            current.push(component);
            let metadata = std::fs::symlink_metadata(&current).map_err(crate::io)?;
            if metadata.file_type().is_symlink() || !metadata.is_dir() {
                return Err(Error::Boundary);
            }
            let leaf = index + 1 == components.len();
            if !leaf && (metadata.uid() != 0 || metadata.mode() & 0o022 != 0) {
                return Err(Error::Boundary);
            }
            if leaf {
                if readonly {
                    if metadata.uid() != 0 || metadata.mode() & 0o222 != 0 {
                        return Err(Error::Boundary);
                    }
                } else if metadata.uid() != owner || metadata.mode() & 0o077 != 0 {
                    return Err(Error::Boundary);
                }
            }
        }
        Ok(())
    }
    pub fn enter(boundary: &Boundary) -> Result<(), Error> {
        boundary.validate()?;
        if unsafe { geteuid() } != 0 {
            return Err(Error::Boundary);
        }
        // Namespace and chroot setup assumes a trusted immutable image layout.
        // Do not derive source paths from a session ID or wire message.
        let sandbox_root = match boundary.role {
            Role::Harness => "/opt/havenbox/sandbox-harness",
            Role::Worker => "/opt/havenbox/sandbox-worker",
        };
        for path in ["/run/hb-sandbox", sandbox_root, "/data/active"] {
            let p = std::path::Path::new(path);
            if p.canonicalize().map_err(crate::io)? != p {
                return Err(Error::Boundary);
            }
            let metadata = std::fs::symlink_metadata(p).map_err(crate::io)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_dir()
                || metadata.uid() != 0
                || metadata.mode() & 0o022 != 0
            {
                return Err(Error::Boundary);
            }
        }
        checked(unsafe { unshare(0x00020000 | 0x40000000 | 0x08000000) })?; // mount, net, IPC
        mount_at("", "/", "", (1 << 18) | 16384, "")?; // private recursive propagation
        mount_at(sandbox_root, "/run/hb-sandbox", "", 4096, "")?;
        mount_at("", "/run/hb-sandbox", "", 4096 | 32 | 1 | 2 | 4, "")?;
        match boundary.role {
            Role::Harness => {
                bind(
                    "/data/active/context",
                    "/run/hb-sandbox/context",
                    true,
                    boundary.uid,
                )?;
                bind(
                    "/data/active/history",
                    "/run/hb-sandbox/history",
                    false,
                    boundary.uid,
                )?;
            }
            Role::Worker => {
                bind(
                    "/data/active/input",
                    "/run/hb-sandbox/input",
                    true,
                    boundary.uid,
                )?;
                bind(
                    "/data/active/output",
                    "/run/hb-sandbox/output",
                    false,
                    boundary.uid,
                )?;
            }
        }
        mount_at(
            "tmpfs",
            "/run/hb-sandbox/tmp",
            "tmpfs",
            2 | 4 | 8,
            &format!(
                "size={},mode=0700,uid={},gid={}",
                boundary.scratch_bytes, boundary.uid, boundary.uid
            ),
        )?;
        checked(unsafe { chroot(c("/run/hb-sandbox").as_ptr()) })?;
        checked(unsafe { chdir(c("/").as_ptr()) })?;
        // Empty bounding set prevents privilege regain through executable file capabilities.
        for capability in 0..=40 {
            checked(unsafe { prctl(24, capability, 0, 0, 0) })?;
        }
        checked(unsafe { prctl(38, 1, 0, 0, 0) })?; // NO_NEW_PRIVS
        checked(unsafe { setgroups(0, std::ptr::null()) })?;
        checked(unsafe { setgid(boundary.uid) })?;
        checked(unsafe { setuid(boundary.uid) })?;
        for (resource, maximum) in [(1, boundary.scratch_bytes), (4, 0), (7, 64)] {
            checked(unsafe {
                setrlimit(
                    resource,
                    &Limit {
                        current: maximum,
                        maximum,
                    },
                )
            })?;
        }
        Ok(())
    }
}

/// # Safety
/// Must run in a fresh child with no inherited authority-bearing file descriptors,
/// already placed in the configured cgroup. On Err, exit immediately, never exec.
/// Parent must independently enforce the lease and kill the cgroup on all failures.
/// Installed sandbox-root and active task mounts must be supervisor-owned, symlink-free,
/// non-writable to children, and contain no devices, procfs or other session data.
pub unsafe fn enter(boundary: &Boundary) -> Result<(), Error> {
    #[cfg(target_os = "linux")]
    {
        linux::enter(boundary)
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = boundary;
        Err(Error::Unsupported)
    }
}
