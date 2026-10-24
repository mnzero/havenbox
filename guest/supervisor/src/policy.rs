#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use crate::{Boundary, Role};

pub(crate) const MIB: u64 = 1_048_576;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct StreamLimits {
    pub(crate) stdout_bytes: usize,
    pub(crate) stderr_bytes: usize,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct OutputQuotaLimit {
    pub(crate) project_id: u32,
    pub(crate) allocated_bytes: u64,
    pub(crate) inodes: u64,
}

pub(crate) struct SyntheticProfile {
    boundary: Boundary,
    streams: StreamLimits,
    output: Option<OutputQuotaLimit>,
}

impl SyntheticProfile {
    pub(crate) fn for_role(role: Role) -> Self {
        let (uid, visible, output) = match role {
            Role::Harness => (
                10_001,
                vec!["/context".into(), "/history".into(), "/tmp".into()],
                None,
            ),
            Role::Worker => (
                10_002,
                vec!["/input".into(), "/output".into(), "/tmp".into()],
                Some(OutputQuotaLimit {
                    project_id: 10_002,
                    allocated_bytes: 64 * MIB,
                    inodes: 4_096,
                }),
            ),
        };
        Self {
            boundary: Boundary {
                role,
                uid,
                input_readonly: true,
                visible,
                memory_bytes: 512 * MIB,
                pids: 64,
                scratch_bytes: 2_048 * MIB,
            },
            streams: StreamLimits {
                stdout_bytes: MIB as usize,
                stderr_bytes: MIB as usize,
            },
            output,
        }
    }

    pub(crate) fn boundary(&self) -> &Boundary {
        &self.boundary
    }

    pub(crate) const fn stream_limits(&self) -> StreamLimits {
        self.streams
    }

    pub(crate) const fn output_limit(&self) -> Option<OutputQuotaLimit> {
        self.output
    }

    #[cfg(test)]
    pub(crate) fn matches_boundary(boundary: &Boundary) -> bool {
        Self::for_role(boundary.role).boundary == *boundary
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_profiles_are_the_only_valid_shapes() {
        let harness = SyntheticProfile::for_role(Role::Harness);
        assert!(SyntheticProfile::matches_boundary(harness.boundary()));
        assert_eq!(harness.output_limit(), None);
        assert_eq!(harness.stream_limits().stdout_bytes, MIB as usize);

        let worker = SyntheticProfile::for_role(Role::Worker);
        assert!(SyntheticProfile::matches_boundary(worker.boundary()));
        assert_eq!(
            worker.output_limit(),
            Some(OutputQuotaLimit {
                project_id: 10_002,
                allocated_bytes: 64 * MIB,
                inodes: 4_096,
            })
        );
        assert_eq!(worker.stream_limits().stderr_bytes, MIB as usize);
    }
}
