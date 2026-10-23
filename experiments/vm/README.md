# HB-02 VM probe

`run.sh self-test` executes bounded frame checks plus a real OS pipe test. It verifies unknown kinds, stale epochs, truncated/oversized frames, invalid control actions, negative and boolean offsets, empty/oversized transfer IDs, cancellation frames, cancelled transfers, and cancellation of a blocked read. Its JSON result explicitly says `vm_boundary: unverified`.

`run.sh launch` requires all of `--image`, `--data`, `--kernel`, `--initrd`, and `--payload`. Image/data/payload/kernel/initramfs must be regular non-symlink files with bounded sizes; the image uses a `.raw` or `.img` suffix and the data disk has a minimum capacity. Memory is limited to 128M..8192M, CPUs to 1..8. Missing or invalid inputs, QEMU, or a failed QEMU version probe return `blocked` (exit 3).

The launch path requires a real ARM64 kernel/initramfs and guest payload. It uses `virt`, `hvf`, `cpu host`, `-nodefaults`, serial stdio, a read-only root image, a writable persistent raw virtio disk, and an explicit `fw_cfg` payload. It has no `-nic`, `-netdev`, QMP, shared folders, or host directory mounts. This script does not claim guest isolation or VM success: those require running QEMU with real artifacts and recording guest probe output, device/mount/network checks, and reboot persistence.
