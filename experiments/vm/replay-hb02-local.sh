#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
tools="$root/tools"
assets="$root/assets/hb02-replay"
cache="$root/assets/hb02-cache/downloads"
data="$root/state/hb02-replay-data.raw"
utm=/Volumes/UTM/UTM.app
utm_volume=/Volumes/UTM

usage() {
  echo "usage: $0 verify|mount|start|kernel-probe-start|stop" >&2
  exit 2
}

verify_hash() {
  local expected=$1 file=$2
  [[ -f "$file" && ! -L "$file" ]] || { echo "blocked: missing regular file: $file" >&2; exit 3; }
  printf '%s  %s\n' "$expected" "$file" | shasum -a 256 -c -
}

mount_utm() {
  local dmg="$tools/UTM-5.0.8.dmg"
  if [[ ! -d "$utm" ]]; then
    verify_hash 7b2d7b2e2a02b3de1757ef19a8f36573e6d9cceae8c8cca0abcbcaa4a852019a \
      "$dmg"
    hdiutil attach -readonly -nobrowse "$dmg" >/dev/null
  fi
  [[ -d "$utm" ]] || { echo "blocked: UTM.app was not mounted at $utm" >&2; exit 3; }
  hdiutil info -plist | plutil -convert json -o - - | python3 -c '
import json, pathlib, sys
dmg, mount = map(str, map(pathlib.Path, sys.argv[1:]))
matches = [image for image in json.load(sys.stdin).get("images", [])
           if image.get("image-path") == dmg and not image.get("writeable", True)
           and any(entity.get("mount-point") == mount
                   for entity in image.get("system-entities", []))]
raise SystemExit(0 if len(matches) == 1 else 3)
' "$dmg" "$utm_volume" || { echo "blocked: UTM mount is not the pinned read-only DMG" >&2; exit 3; }
  mount | grep -F " on /Volumes/UTM " | grep -F 'read-only' >/dev/null || {
    echo "blocked: UTM filesystem is not read-only" >&2
    exit 3
  }
  codesign --verify --deep --strict --all-architectures "$utm" || {
    echo "blocked: UTM bundle signature rejected" >&2
    exit 3
  }
}

verify() {
  verify_hash 7b2d7b2e2a02b3de1757ef19a8f36573e6d9cceae8c8cca0abcbcaa4a852019a "$tools/UTM-5.0.8.dmg"
  verify_hash 2e4246e82d3269b9453ce93cd38df99edd717bfbec721a152dc71e36f1d0ae29 "$tools/qemu-system-aarch64"
  verify_hash 28741481ba33d8e6dc93e3a07e89b03bfbf87695db919ee71b3fc4d56e32f651 "$tools/qemu-img"
  verify_hash a7805d9c373c99038ce79addef9804dc7c849bb8f75cffb705e7a9e1e4b02a3e \
    "$tools/qemu-aarch64-softmmu"
  verify_hash 123fdd935d9a065c58e039bd11f5804f2ce86f3375950c510307a5588d9ed9fa \
    "$tools/qemu-img-framework"
  verify_hash a83d76bb061395b2994ec54b01b6b684f348a786b7e2807b82a8e9e701cc73de "$tools/mke2fs"
  verify_hash 4d3f80afaebd447c5591a63a73bde380a149fe8b2d85488cafabb2ff97551f50 \
    "$cache/alpine-minirootfs-3.24.3-aarch64.tar.gz"
  verify_hash b46c51ddf572d85b2ba7b9adfaa64f4c38fc490c93d97a20ffd2418d51284085 \
    "$cache/linux-virt-6.18.58-r0.apk"
  verify_hash 849394914e6a93cc9bb17e901f445ce21fcd7e4528541d9496a74d6a6ccee319 "$assets/Image"
  verify_hash f27f24052b2682ed16b9ba69fd2f8225e9e104fe571c7865b3f2b91d85a8f5ad "$assets/initramfs-hb02"
  verify_hash ff609bc0022896f18ea0eed6e8aa1e2199490814f1df32474d73f5dc0a612057 "$assets/root.raw"
  verify_hash ce1809621bb6d9fa9eaa86100b25a45fb3a740cbe0d8d826231802f44f8b3ece "$data"
  verify_hash db8ed61038a602d8ea53d5225d96c46e577b1dcaef4154d3af0deff7292572e1 "$root/hb11-kernel-probe.sh"
  mount_utm
  HB02_QEMU_SYSTEM_FRAMEWORK="$tools/qemu-aarch64-softmmu" \
    "$tools/qemu-system-aarch64" --version | grep -F 'version 10.0.12' >/dev/null
  HB02_QEMU_SYSTEM_FRAMEWORK="$tools/qemu-aarch64-softmmu" \
    "$tools/qemu-system-aarch64" -accel help | grep -Fx hvf >/dev/null
  HB02_QEMU_IMG_FRAMEWORK="$tools/qemu-img-framework" \
    "$tools/qemu-img" info --output=json "$assets/root.raw" >/dev/null
  HB02_QEMU_IMG_FRAMEWORK="$tools/qemu-img-framework" \
    "$tools/qemu-img" info --output=json "$data" >/dev/null
  echo "HB-02 local VM assets verified; UTM QEMU 10.0.12 with HVF is ready"
}

start() {
  verify
  HB02_QEMU_SYSTEM_FRAMEWORK="$tools/qemu-aarch64-softmmu" \
    HB02_QEMU_IMG_FRAMEWORK="$tools/qemu-img-framework" HB02_START_TIMEOUT=60 "$root/start.sh" \
    --qemu "$tools/qemu-system-aarch64" \
    --qemu-img "$tools/qemu-img" \
    --image "$assets/root.raw" \
    --data "$data" \
    --kernel "$assets/Image" \
    --initrd "$assets/initramfs-hb02" \
    --payload "$root/assets/guest-payload.sh" \
    --memory 512M --cpus 2
}

kernel_probe_start() {
  verify
  HB02_QEMU_SYSTEM_FRAMEWORK="$tools/qemu-aarch64-softmmu" \
    HB02_QEMU_IMG_FRAMEWORK="$tools/qemu-img-framework" HB02_START_TIMEOUT=60 "$root/start.sh" \
    --qemu "$tools/qemu-system-aarch64" \
    --qemu-img "$tools/qemu-img" \
    --image "$assets/root.raw" \
    --data "$data" \
    --kernel "$assets/Image" \
    --initrd "$assets/initramfs-hb02" \
    --payload "$root/hb11-kernel-probe.sh" \
    --memory 512M --cpus 2
}

case "${1:-}" in
  verify) verify ;;
  mount) mount_utm ;;
  start) start ;;
  kernel-probe-start) kernel_probe_start ;;
  stop) "$root/stop.sh" ;;
  *) usage ;;
esac
