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
  verify_hash 0cdac65d338d3bd1958df4ab91045b08aa0c7146afce4e372578c729b5cbd806 "$tools/qemu-system-aarch64"
  verify_hash ffd34c8655399f25728de1f5ca6c95b78153d7a5334f88010fc391b244c39c4f "$tools/qemu-img"
  verify_hash 661ad9619bb82d332dbf05906f0ecbcabfc35a561fd6a1cdb513632bc530c8d5 \
    "$tools/qemu-aarch64-softmmu"
  verify_hash f38a5c00ec74eb8e164a7b98458822d28a4b170b9b15e11820f4695ab58b16b2 \
    "$tools/qemu-img-framework"
  verify_hash fbaccbd585defb2ea8a4cc887e2a3b0d8eeead1a87aba8c891133397c826976a "$tools/mke2fs"
  verify_hash da5fd75e391994d52ee0ac939b1bde03811d5ac419fb7548a712e1d0f4415eee "$tools/debugfs"
  verify_hash e08604ae9518a551aa95ff15b274796148455341a87e1cfac96a92ab25f20d2b "$tools/e2fsck"
  verify_hash c998d64f9b58c2235186f8ee75ba602d1fdb2991d94695fd687ab0e8096b6727 "$tools/mke2fs.conf"
  MKE2FS_CONFIG="$tools/mke2fs.conf" "$tools/mke2fs" -V 2>&1 | grep -F '1.47.4' >/dev/null
  "$tools/debugfs" -V 2>&1 | grep -F '1.47.4' >/dev/null
  "$tools/e2fsck" -V 2>&1 | grep -F '1.47.4' >/dev/null
  verify_hash 4d3f80afaebd447c5591a63a73bde380a149fe8b2d85488cafabb2ff97551f50 \
    "$cache/alpine-minirootfs-3.24.3-aarch64.tar.gz"
  verify_hash b46c51ddf572d85b2ba7b9adfaa64f4c38fc490c93d97a20ffd2418d51284085 \
    "$cache/linux-virt-6.18.58-r0.apk"
  verify_hash 849394914e6a93cc9bb17e901f445ce21fcd7e4528541d9496a74d6a6ccee319 "$assets/Image"
  verify_hash 8c6c30aaf4737646496a273cc0cafdb5a7f96de25e23da8cdf8973d479e9b530 "$assets/initramfs-hb02"
  verify_hash 341c74a04a9a88f94a9987809490170d8eba497d3598b2329458724313c2728d "$assets/root.raw"
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
