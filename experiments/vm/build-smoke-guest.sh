#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
output=${1:-"$root/assets"}
output=$(CDPATH= cd -P -- "$(dirname -- "$output")" 2>/dev/null && pwd)/$(basename -- "$output")
[[ "$output" == "$root/assets" || "$output" == "$root/assets"/* ]] || { echo "blocked: output must be below $root/assets" >&2; exit 3; }
export COPYFILE_DISABLE=1
alpine_version=3.24.3
alpine_branch=v3.24
kernel_version=6.18.58-r0
minirootfs=alpine-minirootfs-${alpine_version}-aarch64.tar.gz
kernel_apk=linux-virt-${kernel_version}.apk
minirootfs_sha256=4d3f80afaebd447c5591a63a73bde380a149fe8b2d85488cafabb2ff97551f50
kernel_apk_sha256=b46c51ddf572d85b2ba7b9adfaa64f4c38fc490c93d97a20ffd2418d51284085
base=https://dl-cdn.alpinelinux.org/alpine/${alpine_branch}

mkdir -p "$output/downloads"
download() {
    local url=$1 destination=$2 expected=$3
    if [[ ! -f "$destination" ]] || ! printf '%s  %s\n' "$expected" "$destination" | shasum -a 256 -c - >/dev/null 2>&1; then
        curl -fL --retry 3 --output "$destination" "$url"
    fi
    printf '%s  %s\n' "$expected" "$destination" | shasum -a 256 -c -
}

download "$base/releases/aarch64/$minirootfs" "$output/downloads/$minirootfs" "$minirootfs_sha256"
download "$base/main/aarch64/$kernel_apk" "$output/downloads/$kernel_apk" "$kernel_apk_sha256"

work=$(mktemp -d "${TMPDIR:-/tmp}/hb02-guest.XXXXXX")
cleanup() {
    [[ "$work" == "${TMPDIR:-/tmp}/hb02-guest."* ]] && rm -rf -- "$work"
}
trap cleanup EXIT

mkdir -p "$work/root" "$work/kernel"
tar -xzf "$output/downloads/$minirootfs" -C "$work/root"
tar -xzf "$output/downloads/$kernel_apk" -C "$work/kernel"
install -m 0755 "$root/guest-init.sh" "$work/root/init"
mkdir -p "$work/root/dev" "$work/root/proc" "$work/root/sys" "$work/root/run" "$work/root/tmp" "$work/root/sbin"
install -m 0755 "$root/guest-init.sh" "$work/root/sbin/hb02-guest-init"
mkdir -p "$work/root/lib/modules"
install -m 0644 "$work/kernel/lib/modules/6.18.58-0-virt/kernel/drivers/block/virtio_blk.ko.gz" \
    "$work/root/lib/modules/virtio_blk.ko.gz"
install -m 0644 "$work/kernel/lib/modules/6.18.58-0-virt/kernel/drivers/firmware/qemu_fw_cfg.ko.gz" \
    "$work/root/lib/modules/qemu_fw_cfg.ko.gz"
install -m 0644 "$work/kernel/lib/modules/6.18.58-0-virt/kernel/lib/crc/crc16.ko.gz" \
    "$work/root/lib/modules/crc16.ko.gz"
install -m 0644 "$work/kernel/lib/modules/6.18.58-0-virt/kernel/fs/mbcache.ko.gz" \
    "$work/root/lib/modules/mbcache.ko.gz"
install -m 0644 "$work/kernel/lib/modules/6.18.58-0-virt/kernel/fs/jbd2/jbd2.ko.gz" \
    "$work/root/lib/modules/jbd2.ko.gz"
install -m 0644 "$work/kernel/lib/modules/6.18.58-0-virt/kernel/fs/ext4/ext4.ko.gz" \
    "$work/root/lib/modules/ext4.ko.gz"
find "$work/root" -exec touch -h -t 202610240000 {} +

(cd "$work/root" && find . -print | LC_ALL=C sort | cpio -o -H newc 2>/dev/null) | gzip -9n > "$output/initramfs-hb02"
install -m 0644 "$work/kernel/boot/vmlinuz-virt" "$output/Image"
if command -v mke2fs >/dev/null 2>&1; then
    mke2fs -q -t ext4 -L hb02-root -d "$work/root" "$output/root.raw" 64M
else
    echo "blocked: mke2fs is required to build a real root filesystem image" >&2
    exit 3
fi

python3 - "$output/asset-manifest.json" "$root" "$base/releases/aarch64/$minirootfs" "$output/downloads/$minirootfs" "$base/main/aarch64/$kernel_apk" "$output/downloads/$kernel_apk" "$output/Image" "$output/initramfs-hb02" "$output/root.raw" "$alpine_version" "$kernel_version" "build-smoke-guest.sh" <<'PY'
import hashlib, json, pathlib, sys
manifest, root, *raw = sys.argv[1:]
manifest, root = pathlib.Path(manifest), pathlib.Path(root).resolve()
url_mini, mini, url_kernel, kernel_apk, image, initrd, root_image, alpine_version, kernel_version, generation_command = raw
assets = {}
def add(name, path, url, version, fmt, command):
    path = pathlib.Path(path).resolve()
    assets[name] = {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "sha256Scope": "artifact", "format": fmt, "sourceUrl": url, "version": version, "architecture": "aarch64", "generationCommand": command}
add("minirootfs", mini, url_mini, alpine_version, "tar.gz", generation_command)
add("kernelPackage", kernel_apk, url_kernel, kernel_version, "apk", generation_command)
add("kernel", image, "generated://build-smoke-guest.sh", kernel_version, "raw", generation_command)
add("initrd", initrd, "generated://build-smoke-guest.sh", alpine_version, "cpio.gz", generation_command)
add("image", root_image, "generated://build-smoke-guest.sh", alpine_version, "raw", generation_command)
payload = root / "assets/guest-payload.sh"
payload_sha256 = hashlib.sha256(payload.read_bytes()).hexdigest()
assets["payload"] = {
    "path": payload.relative_to(root).as_posix(),
    "bytes": payload.stat().st_size,
    "sha256": payload_sha256,
    "sha256Scope": "artifact",
    "format": "sh",
    "sourceUrl": "repository://experiments/vm/assets/guest-payload.sh",
    "version": f"sha256:{payload_sha256}",
    "architecture": "posix-sh",
    "generationCommand": "checked-in source",
}
obj = {"schemaVersion": "hb-02.vm-assets.v2", "generatedBy": "experiments/vm/build-smoke-guest.sh", "generationCommand": "experiments/vm/build-smoke-guest.sh", "assets": assets}
manifest.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
print(manifest.read_text(), end="")
PY
