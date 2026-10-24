# HB-02 VM probe

`run.sh self-test` executes bounded frame checks plus a real OS pipe test. It verifies unknown kinds, stale epochs, truncated/oversized frames, invalid control actions, negative and boolean offsets, empty/oversized transfer IDs, cancellation frames, cancelled transfers, and cancellation of a blocked read. Its JSON result explicitly says `vm_boundary: unverified`.

`build-smoke-guest.sh` builds a bootable ARM64 smoke guest from pinned Alpine Linux 3.24.3 minirootfs and `linux-virt` 6.18.58-r0 artifacts after checking their SHA-256 values. Its `/init` loads exactly six modules: `virtio_blk`, `qemu_fw_cfg`, `crc16`, `mbcache`, `jbd2`, and `ext4`; it executes the payload delivered through `fw_cfg`, and checks the network, shared mounts, read-only root disk, writable data disk, and reboot persistence before returning the nonce handshake.

`prepare.sh` creates a persistent raw data disk through `qemu-img create`, then writes a build-time local `asset-manifest.json` with byte sizes and SHA-256 values. Manifest paths are relative asset names and the generated manifest is ignored by git; it is evidence for that local build only. `start.sh`, `stop.sh`, and `clean.sh` provide repeatable lifecycle operations; `clean.sh` removes only runtime state and never source guest artifacts.

`run.sh launch` requires all of `--image`, `--data`, `--kernel`, `--initrd`, and `--payload`. Image/data/payload/kernel/initramfs must be regular non-symlink files with bounded sizes; the image uses a `.raw` or `.img` suffix and the data disk has a minimum capacity. Memory is limited to 128M..8192M, CPUs to 1..8. Missing or invalid inputs, QEMU, or a failed QEMU version probe return `blocked` (exit 3).

The launch path requires a real ARM64 kernel/initramfs and guest payload. Startup sends a fresh nonce through `fw_cfg` key `opt/havenbox.nonce`; guest initialization must read that key and emit exactly `HB02_GUEST_READY <nonce>\n` on its serial stdout only after its initialization is ready. The host checks the complete line and QEMU liveness before emitting its internal readiness record; only then does `start.sh` write the pidfile and announce success. Serial and diagnostic output are prefixed so they cannot impersonate that internal record. Exit, malformed handshake, or `HB02_START_TIMEOUT` (default 30 seconds, maximum 300) cleans up the process group and fails closed.

`python3 experiments/vm/test_lifecycle.py` exercises host startup with explicitly fake QEMU fixtures, including immediate exit, timeout, wrong nonce, forged readiness on stdout/stderr, handshake followed by exit, successful serial response, stale PID protection, symlink state rejection, and preservation of assets after cleanup. These tests are not guest or VM boundary evidence.

The launch path uses `virt`, `hvf`, `cpu host`, `-nodefaults`, serial stdio, a read-only root image, a writable persistent raw virtio disk, and an explicit `fw_cfg` payload. It has no `-nic`, `-netdev`, QMP, shared folders, or host directory mounts.

## Real VM smoke test on Apple Silicon

The checked-in launcher is a thin wrapper that dynamically loads the UTM QEMU framework with `dlopen`; it does not contain UTM or QEMU. The current workspace does not include `UTM.app`, so the recorded real-launch evidence came from an external/local UTM 5.0.8 environment and cannot be independently replayed from this workspace alone.

If the external UTM app, its QEMU frameworks, the macOS SDK, `clang`, or `codesign` is unavailable, the launcher build exits 3 with `blocked`; no launcher or VM pass claim is produced.

```sh
experiments/vm/build-utm-launchers.sh /Applications/UTM.app
experiments/vm/build-smoke-guest.sh
export HB02_UTM_APP=/Applications/UTM.app
export QEMU_IMG=$PWD/experiments/vm/tools/qemu-img

experiments/vm/prepare.sh \
  --image assets/root.raw \
  --kernel assets/Image \
  --initrd assets/initramfs-hb02

experiments/vm/start.sh \
  --qemu experiments/vm/tools/qemu-system-aarch64 \
  --qemu-img experiments/vm/tools/qemu-img \
  --image experiments/vm/assets/root.raw \
  --data experiments/vm/state/data.raw \
  --kernel experiments/vm/assets/Image \
  --initrd experiments/vm/assets/initramfs-hb02 \
  --payload experiments/vm/assets/guest-payload.sh \
  --memory 512M --cpus 2
```

After `stop.sh`, repeat `start.sh`. The first guest probe reports `persistence=created`; the second reports `persistence=present`. A successful launch also reports `network=loopback-only`, `root_readonly=true`, `data_device=/dev/vdb`, and `shared_mounts=none`. The host launch record independently shows that no network backend, QMP endpoint, or shared-directory device was configured.
