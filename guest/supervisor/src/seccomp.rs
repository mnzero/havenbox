#![cfg_attr(not(target_os = "linux"), allow(dead_code))]

use crate::Error;

pub(crate) const AUDIT_ARCH_AARCH64: u32 = 0xc000_00b7;
const SECCOMP_RET_KILL_PROCESS: u32 = 0x8000_0000;
const SECCOMP_RET_ALLOW: u32 = 0x7fff_0000;

const BPF_LD_W_ABS: u16 = 0x20;
const BPF_JMP_JEQ_K: u16 = 0x15;
const BPF_RET_K: u16 = 0x06;

// Linux aarch64 syscall numbers needed by the fixed BusyBox/musl synthetic probe.
// This candidate list is fail-closed and still requires tracing in the pinned guest.
const ALLOWED_SYSCALLS: &[u32] = &[
    17, 23, 24, 25, 29, 48, 49, 56, 57, 59, 61, 62, 63, 64, 65, 66, 67, 72, 73, 78, 79, 80, 93, 94,
    96, 98, 99, 113, 129, 131, 132, 134, 135, 139, 144, 146, 153, 158, 160, 172, 173, 174, 175,
    176, 177, 178, 214, 215, 216, 221, 222, 226, 233, 260, 261, 278, 293,
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(C)]
pub(crate) struct BpfInstruction {
    pub(crate) code: u16,
    pub(crate) jt: u8,
    pub(crate) jf: u8,
    pub(crate) k: u32,
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Decision {
    Allow,
    KillProcess,
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct SyscallInput {
    pub(crate) audit_arch: u32,
    pub(crate) number: u32,
}

#[cfg(test)]
pub(crate) fn decision(input: SyscallInput) -> Decision {
    if input.audit_arch == AUDIT_ARCH_AARCH64 && ALLOWED_SYSCALLS.contains(&input.number) {
        Decision::Allow
    } else {
        Decision::KillProcess
    }
}

pub(crate) fn synthetic_program() -> Vec<BpfInstruction> {
    let mut program = Vec::with_capacity(5 + ALLOWED_SYSCALLS.len() * 2);
    program.push(statement(BPF_LD_W_ABS, 4));
    program.push(jump(BPF_JMP_JEQ_K, AUDIT_ARCH_AARCH64, 1, 0));
    program.push(statement(BPF_RET_K, SECCOMP_RET_KILL_PROCESS));
    program.push(statement(BPF_LD_W_ABS, 0));
    for number in ALLOWED_SYSCALLS {
        program.push(jump(BPF_JMP_JEQ_K, *number, 0, 1));
        program.push(statement(BPF_RET_K, SECCOMP_RET_ALLOW));
    }
    program.push(statement(BPF_RET_K, SECCOMP_RET_KILL_PROCESS));
    program
}

const fn statement(code: u16, k: u32) -> BpfInstruction {
    BpfInstruction {
        code,
        jt: 0,
        jf: 0,
        k,
    }
}

const fn jump(code: u16, k: u32, jt: u8, jf: u8) -> BpfInstruction {
    BpfInstruction { code, jt, jf, k }
}

#[cfg(all(target_os = "linux", target_arch = "aarch64"))]
pub(crate) fn install_synthetic() -> Result<(), Error> {
    use std::ffi::{c_int, c_ulong};

    #[repr(C)]
    struct SockFprog {
        len: u16,
        filter: *const BpfInstruction,
    }
    unsafe extern "C" {
        fn prctl(option: c_int, ...) -> c_int;
    }
    const PR_SET_SECCOMP: c_int = 22;
    const SECCOMP_MODE_FILTER: c_ulong = 2;

    let program = synthetic_program();
    let filter = SockFprog {
        len: u16::try_from(program.len()).map_err(|_| Error::Boundary)?,
        filter: program.as_ptr(),
    };
    if std::mem::size_of::<BpfInstruction>() != 8 || std::mem::size_of::<SockFprog>() != 16 {
        return Err(Error::Boundary);
    }
    let result = unsafe { prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &filter) };
    if result == 0 {
        Ok(())
    } else {
        Err(Error::Io(std::io::Error::last_os_error().to_string()))
    }
}

#[cfg(not(all(target_os = "linux", target_arch = "aarch64")))]
pub(crate) fn install_synthetic() -> Result<(), Error> {
    Err(Error::Unsupported)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn interpret(program: &[BpfInstruction], input: SyscallInput) -> Decision {
        let mut accumulator = 0_u32;
        let mut pc = 0_usize;
        loop {
            let instruction = program[pc];
            match instruction.code {
                BPF_LD_W_ABS => {
                    accumulator = match instruction.k {
                        0 => input.number,
                        4 => input.audit_arch,
                        _ => return Decision::KillProcess,
                    };
                    pc += 1;
                }
                BPF_JMP_JEQ_K => {
                    pc += 1 + if accumulator == instruction.k {
                        instruction.jt as usize
                    } else {
                        instruction.jf as usize
                    };
                }
                BPF_RET_K => {
                    return if instruction.k == SECCOMP_RET_ALLOW {
                        Decision::Allow
                    } else {
                        Decision::KillProcess
                    };
                }
                _ => return Decision::KillProcess,
            }
        }
    }

    #[test]
    fn generated_filter_matches_the_fixed_policy() {
        let program = synthetic_program();
        assert_eq!(std::mem::size_of::<BpfInstruction>(), 8);
        for number in 0..=450 {
            for arch in [AUDIT_ARCH_AARCH64, 0, 0xc000_003e] {
                let input = SyscallInput {
                    audit_arch: arch,
                    number,
                };
                assert_eq!(interpret(&program, input), decision(input));
            }
        }
        assert_eq!(
            decision(SyscallInput {
                audit_arch: AUDIT_ARCH_AARCH64,
                number: 198,
            }),
            Decision::KillProcess
        );
    }
}
