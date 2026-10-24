#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use crate::{Error, policy::OutputQuotaLimit};

pub(crate) const QUOTA_BLOCK_BYTES: u64 = 1_024;

pub(crate) fn hard_blocks(limit: OutputQuotaLimit) -> Result<u64, Error> {
    if limit.project_id == 0
        || limit.allocated_bytes == 0
        || limit.allocated_bytes % QUOTA_BLOCK_BYTES != 0
        || limit.inodes == 0
    {
        return Err(Error::Boundary);
    }
    Ok(limit.allocated_bytes / QUOTA_BLOCK_BYTES)
}

pub(crate) struct WorkerOutputQuota {
    #[cfg(target_os = "linux")]
    directory: std::fs::File,
    #[cfg(target_os = "linux")]
    restored: bool,
}

impl WorkerOutputQuota {
    pub(crate) fn prepare(limit: OutputQuotaLimit) -> Result<Self, Error> {
        #[cfg(target_os = "linux")]
        {
            linux::prepare(limit)
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = limit;
            Err(Error::Unsupported)
        }
    }

    pub(crate) fn restore(&mut self) -> Result<(), Error> {
        #[cfg(target_os = "linux")]
        {
            if !self.restored {
                linux::restore(&self.directory)?;
                self.restored = true;
            }
            Ok(())
        }
        #[cfg(not(target_os = "linux"))]
        {
            Err(Error::Unsupported)
        }
    }
}

impl Drop for WorkerOutputQuota {
    fn drop(&mut self) {
        #[cfg(target_os = "linux")]
        if !self.restored {
            let _ = linux::restore(&self.directory);
        }
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use std::ffi::{CString, c_char, c_int, c_ulong, c_void};
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

    const OUTPUT: &str = "/data/active/output";
    const DEVICE: &str = "/dev/vdb";
    const WORKER_UID: u32 = 10_002;
    const O_DIRECTORY: c_int = 0x4000;
    const O_NOFOLLOW: c_int = 0x20000;
    const O_CLOEXEC: c_int = 0x80000;
    const FS_IOC_FSGETXATTR: c_ulong = 0x801c_581f;
    const FS_IOC_FSSETXATTR: c_ulong = 0x401c_5820;
    const FS_XFLAG_PROJINHERIT: u32 = 0x0000_0200;
    const PRJQUOTA: u32 = 2;
    const Q_GETQUOTA: u32 = 0x8000_07;
    const Q_SETQUOTA: u32 = 0x8000_08;
    const QIF_BLIMITS: u32 = 1;
    const QIF_ILIMITS: u32 = 4;

    #[repr(C)]
    #[derive(Default)]
    struct Fsxattr {
        fsx_xflags: u32,
        fsx_extsize: u32,
        fsx_nextents: u32,
        fsx_projid: u32,
        fsx_cowextsize: u32,
        fsx_pad: [u8; 8],
    }

    #[repr(C)]
    #[derive(Default)]
    struct IfDqblk {
        dqb_bhardlimit: u64,
        dqb_bsoftlimit: u64,
        dqb_curspace: u64,
        dqb_ihardlimit: u64,
        dqb_isoftlimit: u64,
        dqb_curinodes: u64,
        dqb_btime: u64,
        dqb_itime: u64,
        dqb_valid: u32,
    }

    unsafe extern "C" {
        fn ioctl(fd: c_int, request: c_ulong, ...) -> c_int;
        fn quotactl(cmd: c_int, special: *const c_char, id: c_int, addr: *mut c_char) -> c_int;
        fn fchown(fd: c_int, owner: u32, group: u32) -> c_int;
        fn fchmod(fd: c_int, mode: u32) -> c_int;
    }

    const fn qcmd(command: u32, quota_type: u32) -> c_int {
        ((command << 8) | quota_type) as c_int
    }

    pub(super) fn prepare(limit: OutputQuotaLimit) -> Result<WorkerOutputQuota, Error> {
        let blocks = hard_blocks(limit)?;
        require_prjquota_mount()?;
        let metadata = std::fs::symlink_metadata(OUTPUT).map_err(crate::io)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || metadata.uid() != 0
            || metadata.mode() & 0o777 != 0o700
            || std::fs::read_dir(OUTPUT)
                .map_err(crate::io)?
                .next()
                .is_some()
        {
            return Err(Error::Boundary);
        }
        let directory = std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            .open(OUTPUT)
            .map_err(crate::io)?;
        let fd = directory.as_raw_fd();

        let mut attributes = Fsxattr::default();
        checked(unsafe { ioctl(fd, FS_IOC_FSGETXATTR, &mut attributes) })?;
        if attributes.fsx_projid != 0 && attributes.fsx_projid != limit.project_id {
            return Err(Error::Boundary);
        }
        attributes.fsx_projid = limit.project_id;
        attributes.fsx_xflags |= FS_XFLAG_PROJINHERIT;
        checked(unsafe { ioctl(fd, FS_IOC_FSSETXATTR, &attributes) })?;
        let mut verified = Fsxattr::default();
        checked(unsafe { ioctl(fd, FS_IOC_FSGETXATTR, &mut verified) })?;
        if verified.fsx_projid != limit.project_id
            || verified.fsx_xflags & FS_XFLAG_PROJINHERIT == 0
        {
            return Err(Error::Resource);
        }

        let device = CString::new(DEVICE).expect("fixed quota device");
        let mut quota = IfDqblk {
            dqb_bhardlimit: blocks,
            dqb_ihardlimit: limit.inodes,
            dqb_valid: QIF_BLIMITS | QIF_ILIMITS,
            ..IfDqblk::default()
        };
        checked(unsafe {
            quotactl(
                qcmd(Q_SETQUOTA, PRJQUOTA),
                device.as_ptr(),
                limit.project_id as c_int,
                (&mut quota as *mut IfDqblk)
                    .cast::<c_void>()
                    .cast::<c_char>(),
            )
        })?;
        let mut readback = IfDqblk::default();
        checked(unsafe {
            quotactl(
                qcmd(Q_GETQUOTA, PRJQUOTA),
                device.as_ptr(),
                limit.project_id as c_int,
                (&mut readback as *mut IfDqblk)
                    .cast::<c_void>()
                    .cast::<c_char>(),
            )
        })?;
        if readback.dqb_bhardlimit != blocks
            || readback.dqb_ihardlimit != limit.inodes
            || readback.dqb_curspace > limit.allocated_bytes
            || readback.dqb_curinodes > limit.inodes
        {
            return Err(Error::Resource);
        }

        checked(unsafe { fchown(fd, WORKER_UID, WORKER_UID) })?;
        if let Err(error) = checked(unsafe { fchmod(fd, 0o700) }) {
            let _ = restore(&directory);
            return Err(error);
        }
        let final_metadata = directory.metadata().map_err(crate::io)?;
        if final_metadata.uid() != WORKER_UID || final_metadata.mode() & 0o777 != 0o700 {
            let _ = restore(&directory);
            return Err(Error::Boundary);
        }
        Ok(WorkerOutputQuota {
            directory,
            restored: false,
        })
    }

    pub(super) fn restore(directory: &std::fs::File) -> Result<(), Error> {
        let fd = directory.as_raw_fd();
        checked(unsafe { fchown(fd, 0, 0) })?;
        checked(unsafe { fchmod(fd, 0o700) })?;
        let metadata = directory.metadata().map_err(crate::io)?;
        if metadata.uid() == 0 && metadata.mode() & 0o777 == 0o700 {
            Ok(())
        } else {
            Err(Error::Boundary)
        }
    }

    fn require_prjquota_mount() -> Result<(), Error> {
        let mountinfo = std::fs::read_to_string("/proc/self/mountinfo").map_err(crate::io)?;
        let matches = mountinfo.lines().filter(|line| {
            line.split_once(" - ").is_some_and(|(left, right)| {
                let fields: Vec<_> = left.split(' ').collect();
                let after: Vec<_> = right.split(' ').collect();
                fields.get(4) == Some(&"/data")
                    && after.first() == Some(&"ext4")
                    && after.get(1) == Some(&DEVICE)
                    && after
                        .get(2)
                        .is_some_and(|options| options.split(',').any(|value| value == "prjquota"))
            })
        });
        if matches.count() == 1 {
            Ok(())
        } else {
            Err(Error::Boundary)
        }
    }

    fn checked(result: c_int) -> Result<(), Error> {
        if result == 0 {
            Ok(())
        } else {
            Err(Error::Io(std::io::Error::last_os_error().to_string()))
        }
    }

    const _: () = assert!(std::mem::size_of::<Fsxattr>() == 28);
    const _: () = assert!(std::mem::size_of::<IfDqblk>() == 72);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quota_conversion_is_exact_and_rejects_invalid_limits() {
        let valid = OutputQuotaLimit {
            project_id: 10_002,
            allocated_bytes: 64 * 1_048_576,
            inodes: 4_096,
        };
        assert_eq!(hard_blocks(valid), Ok(65_536));
        for invalid in [
            OutputQuotaLimit {
                project_id: 0,
                ..valid
            },
            OutputQuotaLimit {
                allocated_bytes: 1,
                ..valid
            },
            OutputQuotaLimit { inodes: 0, ..valid },
        ] {
            assert_eq!(hard_blocks(invalid), Err(Error::Boundary));
        }
    }

    #[test]
    fn unsupported_host_cannot_claim_quota_enforcement() {
        if !cfg!(target_os = "linux") {
            let limit = SyntheticQuota::worker();
            assert!(matches!(
                WorkerOutputQuota::prepare(limit),
                Err(Error::Unsupported)
            ));
        }
    }

    struct SyntheticQuota;
    impl SyntheticQuota {
        fn worker() -> OutputQuotaLimit {
            OutputQuotaLimit {
                project_id: 10_002,
                allocated_bytes: 64 * 1_048_576,
                inodes: 4_096,
            }
        }
    }
}
