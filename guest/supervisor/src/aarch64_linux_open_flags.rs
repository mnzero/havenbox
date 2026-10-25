//! Linux `open(2)` flags for the AArch64 UAPI used by the guest runtime.
#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

pub(crate) const O_DIRECTORY: i32 = 1 << 14;
pub(crate) const O_NOFOLLOW: i32 = 1 << 15;
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) const O_DIRECT: i32 = 1 << 16;
pub(crate) const O_CLOEXEC: i32 = 1 << 19;

pub(crate) const NOFOLLOW_OPEN_FLAGS: i32 = O_NOFOLLOW | O_CLOEXEC;
pub(crate) const DIRECTORY_OPEN_FLAGS: i32 = O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn values_match_linux_aarch64_uapi_and_are_disjoint() {
        assert_eq!(O_DIRECTORY, 1 << 14);
        assert_eq!(O_NOFOLLOW, 1 << 15);
        assert_eq!(O_DIRECT, 1 << 16);
        assert_eq!(O_CLOEXEC, 1 << 19);

        let values = [O_DIRECTORY, O_NOFOLLOW, O_DIRECT, O_CLOEXEC];
        for (index, value) in values.iter().enumerate() {
            for other in &values[index + 1..] {
                assert_eq!(value & other, 0);
            }
        }
    }

    #[test]
    fn directory_open_flags_exclude_direct_and_largefile() {
        const O_LARGEFILE: i32 = 1 << 17;
        assert_eq!(NOFOLLOW_OPEN_FLAGS, O_NOFOLLOW | O_CLOEXEC);
        assert_eq!(DIRECTORY_OPEN_FLAGS & O_DIRECT, 0);
        assert_eq!(DIRECTORY_OPEN_FLAGS & O_LARGEFILE, 0);
    }

    #[test]
    fn security_sensitive_callers_share_the_aarch64_directory_flags() {
        assert_eq!(
            crate::task_channel::AARCH64_LINUX_DIRECTORY_OPEN_FLAGS,
            DIRECTORY_OPEN_FLAGS
        );
        assert_eq!(
            crate::quota::AARCH64_LINUX_DIRECTORY_OPEN_FLAGS,
            DIRECTORY_OPEN_FLAGS
        );
    }
}
