#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
usage() {
  printf '%s\n' "usage: $0 --minirootfs FILE --kernel-apk FILE --supervisor FILE --output DIR --mke2fs FILE --mke2fs-config FILE --debugfs FILE --e2fsck FILE" >&2
  exit 2
}

minirootfs=
kernel_apk=
supervisor=
output=
mke2fs=
mke2fs_config=
debugfs=
e2fsck=
while (($#)); do
  case "$1" in
    --minirootfs) minirootfs=${2:-}; shift 2 ;;
    --kernel-apk) kernel_apk=${2:-}; shift 2 ;;
    --supervisor) supervisor=${2:-}; shift 2 ;;
    --output) output=${2:-}; shift 2 ;;
    --mke2fs) mke2fs=${2:-}; shift 2 ;;
    --mke2fs-config) mke2fs_config=${2:-}; shift 2 ;;
    --debugfs) debugfs=${2:-}; shift 2 ;;
    --e2fsck) e2fsck=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ -f "$minirootfs" && -f "$kernel_apk" && -f "$supervisor" && -n "$output" ]] || usage
[[ -n "$mke2fs" && -f "$mke2fs" && ! -L "$mke2fs" && -x "$mke2fs" ]] || {
  echo "blocked: image build requires an explicit pinned mke2fs" >&2
  exit 3
}
[[ -n "$mke2fs_config" && -f "$mke2fs_config" && ! -L "$mke2fs_config" ]] || {
  echo "blocked: image build requires an explicit pinned mke2fs.conf" >&2
  exit 3
}
[[ -n "$debugfs" && -f "$debugfs" && ! -L "$debugfs" && -x "$debugfs" ]] || {
  echo "blocked: image build requires a pinned debugfs for ownership verification" >&2
  exit 3
}
[[ -n "$e2fsck" && -f "$e2fsck" && ! -L "$e2fsck" && -x "$e2fsck" ]] || {
  echo "blocked: image build requires a pinned e2fsck" >&2
  exit 3
}
[[ ! -e "$output" ]] || { echo "blocked: output already exists" >&2; exit 3; }
for command in file gzip python3 tar shasum; do
  command -v "$command" >/dev/null || { echo "blocked: $command is required" >&2; exit 3; }
done

mini_sha=4d3f80afaebd447c5591a63a73bde380a149fe8b2d85488cafabb2ff97551f50
kernel_sha=b46c51ddf572d85b2ba7b9adfaa64f4c38fc490c93d97a20ffd2418d51284085
printf '%s  %s\n' "$mini_sha" "$minirootfs" | shasum -a 256 -c -
printf '%s  %s\n' "$kernel_sha" "$kernel_apk" | shasum -a 256 -c -
supervisor_kind=$(file -b "$supervisor")
[[ "$supervisor_kind" == *"ELF 64-bit LSB"* && "$supervisor_kind" == *"ARM aarch64"* && "$supervisor_kind" == *"statically linked"* ]] || {
  echo "blocked: supervisor must be a static Linux aarch64 ELF" >&2
  exit 3
}

work=$(mktemp -d "${TMPDIR:-/tmp}/hb11-image.XXXXXX")
cleanup() {
  if [[ "$work" == "${TMPDIR:-/tmp}/hb11-image."* ]]; then
    chmod -R u+w "$work" 2>/dev/null || true
    rm -rf -- "$work"
  fi
}
trap cleanup EXIT
artifacts="$work/output"
mkdir -p "$work/root" "$work/kernel" "$work/init/lib/modules" "$artifacts"
tar -xzf "$minirootfs" -C "$work/root"
tar -xzf "$kernel_apk" -C "$work/kernel"

install -m 0755 "$root/guest/image/hb-init" "$work/root/sbin/hb-init"
install -m 0755 "$supervisor" "$work/root/usr/sbin/hb-supervisor"
for sandbox in sandbox-harness sandbox-worker; do
  mkdir -p "$work/root/opt/havenbox/$sandbox/bin" "$work/root/opt/havenbox/$sandbox/lib" \
    "$work/root/opt/havenbox/$sandbox/tmp"
done
mkdir -p "$work/root/opt/havenbox/sandbox-harness/context" \
  "$work/root/opt/havenbox/sandbox-harness/history" \
  "$work/root/opt/havenbox/sandbox-worker/input" \
  "$work/root/opt/havenbox/sandbox-worker/output" \
  "$work/root/dev/pts" "$work/root/proc" "$work/root/sys/fs/cgroup" "$work/root/tmp" "$work/root/data" "$work/root/run/hb-sandbox"
for sandbox in sandbox-harness sandbox-worker; do
  install -m 0755 "$work/root/bin/busybox" "$work/root/opt/havenbox/$sandbox/bin/busybox"
  install -m 0755 "$root/guest/image/hb-synthetic-task" "$work/root/opt/havenbox/$sandbox/bin/hb-synthetic-task"
  for loader in "$work/root"/lib/ld-musl-*.so.1; do
    [[ -f "$loader" ]] || { echo "blocked: Alpine musl loader missing" >&2; exit 3; }
    install -m 0755 "$loader" "$work/root/opt/havenbox/$sandbox/lib/$(basename "$loader")"
  done
  chmod 0555 "$work/root/opt/havenbox/$sandbox" "$work/root/opt/havenbox/$sandbox/bin" "$work/root/opt/havenbox/$sandbox/lib"
done

module_root="$work/kernel/lib/modules/6.18.58-0-virt/kernel"
install_module() {
  local source=$1 name=$2
  install -m 0644 "$module_root/$source" "$work/init/lib/modules/$name.ko.gz"
}
install_module drivers/block/virtio_blk.ko.gz virtio_blk
install_module lib/crc/crc16.ko.gz crc16
install_module fs/mbcache.ko.gz mbcache
install_module fs/jbd2/jbd2.ko.gz jbd2
install_module fs/quota/quota_tree.ko.gz quota_tree
install_module fs/quota/quota_v2.ko.gz quota_v2
install_module fs/ext4/ext4.ko.gz ext4
grep -Fx 'kernel/drivers/char/virtio_console.ko' \
  "$work/kernel/lib/modules/6.18.58-0-virt/modules.builtin" >/dev/null || {
  echo "blocked: virtio_console must be built into the reviewed kernel" >&2
  exit 3
}
install -m 0755 "$root/guest/image/initramfs-init" "$work/init/init"
mkdir -p "$work/init/dev" "$work/init/proc" "$work/init/sys" "$work/init/newroot" "$work/init/bin" "$work/init/sbin"
install -m 0755 "$work/root/bin/busybox" "$work/init/bin/busybox"
mkdir -p "$work/init/lib"
for loader in "$work/root"/lib/ld-musl-*.so.1; do
  [[ -f "$loader" ]] || { echo "blocked: Alpine musl loader missing" >&2; exit 3; }
  install -m 0755 "$loader" "$work/init/lib/$(basename "$loader")"
done
for applet in sh mkdir mount umount insmod sleep switch_root; do ln -s busybox "$work/init/bin/$applet"; done
ln -s ../bin/busybox "$work/init/sbin/poweroff"

export COPYFILE_DISABLE=1
# This is 2026-10-24T00:00:00Z, the frozen candidate-17 source date.
image_epoch=1792800000
# UUIDv5(DNS, "havenbox.dev/hb11/{root,data}.raw") makes the identities stable,
# auditable, and distinct without consulting host randomness.
root_uuid=b0e87f5b-28ba-5ca5-ba8d-2a0e2c18bdc6
data_uuid=50718f8a-aea7-55d6-a82a-ddd4cedd8e5d
TZ=UTC find "$work/root" -exec touch -h -t 202610240000 {} +
TZ=UTC find "$work/init" -exec touch -h -t 202610240000 {} +
python3 "$root/guest/image/deterministic-newc.py" \
  --root "$work/init" --mtime "$image_epoch" | gzip -9n >"$artifacts/initramfs-hb11"
install -m 0644 "$work/kernel/boot/vmlinuz-virt" "$artifacts/Image"
export E2FSPROGS_FAKE_TIME=$image_epoch
export SOURCE_DATE_EPOCH=$image_epoch
MKE2FS_CONFIG="$mke2fs_config" "$mke2fs" -q -t ext4 -L hb11-root -U "$root_uuid" \
  -E "hash_seed=$root_uuid,lazy_itable_init=0,lazy_journal_init=0,nodiscard,root_owner=0:0" \
  -d "$work/root" "$artifacts/root.raw" 256M
MKE2FS_CONFIG="$mke2fs_config" "$mke2fs" -q -t ext4 -O project,quota \
  -E "quotatype=prjquota,hash_seed=$data_uuid,lazy_itable_init=0,lazy_journal_init=0,nodiscard,root_owner=0:0" \
  -L hb11-data -U "$data_uuid" "$artifacts/data.raw" 256M
for filesystem in "$artifacts/root.raw" "$artifacts/data.raw"; do
  set +e
  "$e2fsck" -f -y "$filesystem"
  fsck_status=$?
  set -e
  ((fsck_status == 0 || fsck_status == 1)) || {
    echo "blocked: e2fsck could not repair candidate filesystem" >&2
    exit 3
  }
done
if ((EUID == 0)); then
  python3 "$root/guest/image/normalize-ext4.py" --debugfs "$debugfs" \
    --image "$artifacts/root.raw" --minimum-inodes 100 \
    --expected-uuid "$root_uuid" --timestamp "$image_epoch"
else
  python3 "$root/guest/image/normalize-ext4.py" --debugfs "$debugfs" \
    --image "$artifacts/root.raw" --minimum-inodes 100 --normalize \
    --expected-uuid "$root_uuid" --timestamp "$image_epoch"
fi
python3 "$root/guest/image/normalize-ext4.py" --debugfs "$debugfs" \
  --image "$artifacts/data.raw" --minimum-inodes 2 \
  --expected-uuid "$data_uuid" --timestamp "$image_epoch"
for filesystem in "$artifacts/root.raw" "$artifacts/data.raw"; do
  "$e2fsck" -f -n "$filesystem" >/dev/null || {
    echo "blocked: final ext4 consistency check failed" >&2
    exit 3
  }
done
chmod 0444 "$artifacts/Image" "$artifacts/initramfs-hb11" \
  "$artifacts/root.raw" "$artifacts/data.raw"

python3 - "$artifacts" "$minirootfs" "$kernel_apk" "$supervisor" <<'PY'
import hashlib, json, pathlib, sys
output = pathlib.Path(sys.argv[1])
inputs = [pathlib.Path(value) for value in sys.argv[2:]]
files = inputs + [output / name for name in ['Image', 'initramfs-hb11', 'root.raw', 'data.raw']]
manifest = {
    'schemaVersion': 'hb-11.guest-image.v1',
    'state': 'candidate',
    'files': {path.name: {'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in files},
}
(output / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
PY
mv "$artifacts" "$output"
