#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
utm_app=${1:-/Volumes/UTM/UTM.app}
output=${2:-"$root/tools"}
frameworks="$utm_app/Contents/Frameworks"
system_framework="$frameworks/qemu-aarch64-softmmu.framework/Versions/A/qemu-aarch64-softmmu"
image_framework="$frameworks/qemu-img.framework/Versions/A/qemu-img"
sdk=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
deployment_target=14.0

verify_hash() {
  local expected=$1 file=$2
  [[ -f "$file" && ! -L "$file" ]] || { echo "blocked: missing regular file: $file" >&2; exit 3; }
  printf '%s  %s\n' "$expected" "$file" | shasum -a 256 -c -
}

verify_minos() {
  local file=$1 expected=$2
  local actual
  actual=$(otool -l "$file" | awk '$1 == "minos" { print $2 }' | sort -u)
  [[ "$actual" == "$expected" ]] || {
    echo "blocked: $file has minOS '${actual:-missing}', expected $expected" >&2
    exit 3
  }
}

[[ -d "$frameworks" ]] || { echo "blocked: UTM frameworks not found: $frameworks" >&2; exit 3; }
[[ -d "$sdk" ]] || { echo "blocked: pinned macOS SDK not found: $sdk" >&2; exit 3; }
[[ "$(xcrun --sdk "$sdk" --show-sdk-version)" == 26.5 ]] || {
  echo "blocked: pinned SDK does not report version 26.5" >&2
  exit 3
}
verify_hash 16d48f6e3e83fdabb760ac01f17f2c69af844f3403be77fad51c692189896f5f "$system_framework"
verify_hash dfbb592963ff99cee305222358d02580c2f0898c04306f9d9b926a0e53ad875d "$image_framework"

mkdir -p "$output"
staging=$(mktemp -d "$output/.candidate17-qemu.XXXXXX")
cleanup() {
  [[ "$staging" == "$output/.candidate17-qemu."* ]] && rm -rf -- "$staging"
}
trap cleanup EXIT

build_once() {
  local destination=$1
  mkdir "$destination"
  MACOSX_DEPLOYMENT_TARGET=$deployment_target /usr/bin/clang -O2 -Wall -Wextra -Werror \
    -isysroot "$sdk" -mmacosx-version-min=$deployment_target \
    "$root/utm-qemu-launcher.c" -o "$destination/qemu-system-aarch64" \
    -Wl,-rpath,"$frameworks"
  cp "$destination/qemu-system-aarch64" "$destination/qemu-img"
  codesign --force --sign - --entitlements "$root/utm-qemu.entitlements" \
    "$destination/qemu-system-aarch64"
  codesign --force --sign - "$destination/qemu-img"
  cp "$system_framework" "$destination/qemu-aarch64-softmmu"
  cp "$image_framework" "$destination/qemu-img-framework"
  codesign --force --sign - "$destination/qemu-aarch64-softmmu"
  codesign --force --sign - "$destination/qemu-img-framework"
  chmod 0555 "$destination"/*
}

build_once "$staging/build-1"
build_once "$staging/build-2"

for artifact in qemu-system-aarch64 qemu-img qemu-aarch64-softmmu qemu-img-framework; do
  case "$artifact" in
    qemu-system-aarch64) expected=0cdac65d338d3bd1958df4ab91045b08aa0c7146afce4e372578c729b5cbd806 ;;
    qemu-img) expected=ffd34c8655399f25728de1f5ca6c95b78153d7a5334f88010fc391b244c39c4f ;;
    qemu-aarch64-softmmu) expected=661ad9619bb82d332dbf05906f0ecbcabfc35a561fd6a1cdb513632bc530c8d5 ;;
    qemu-img-framework) expected=f38a5c00ec74eb8e164a7b98458822d28a4b170b9b15e11820f4695ab58b16b2 ;;
  esac
  cmp "$staging/build-1/$artifact" "$staging/build-2/$artifact"
  verify_hash "$expected" "$staging/build-1/$artifact"
  codesign --verify --strict --all-architectures "$staging/build-1/$artifact"
done
verify_minos "$staging/build-1/qemu-system-aarch64" 14.0
verify_minos "$staging/build-1/qemu-img" 14.0
# UTM's frameworks retain their lower compatibility boundary. The normalized
# comparison below proves that ad-hoc signing changes no executable code/data.
verify_minos "$staging/build-1/qemu-aarch64-softmmu" 12.0
verify_minos "$staging/build-1/qemu-img-framework" 12.0
for launcher in qemu-system-aarch64 qemu-img; do
  [[ "$(otool -L "$staging/build-1/$launcher" | tail -n +2 | awk '{print $1}')" == \
    /usr/lib/libSystem.B.dylib ]] || {
    echo "blocked: unexpected dynamic dependency in $launcher" >&2
    exit 3
  }
done
entitlements=$(codesign -d --entitlements :- \
  "$staging/build-1/qemu-system-aarch64" 2>/dev/null | plutil -convert json -o - -)
[[ "$entitlements" == '{"com.apple.security.hypervisor":true}' ]] || {
  echo "blocked: qemu-system-aarch64 entitlements differ from the pinned policy" >&2
  exit 3
}
if codesign -d --entitlements :- "$staging/build-1/qemu-img" 2>/dev/null | grep -q .; then
  echo "blocked: qemu-img must not carry entitlements" >&2
  exit 3
fi

python3 - "$system_framework" "$staging/build-1/qemu-aarch64-softmmu" \
  "$image_framework" "$staging/build-1/qemu-img-framework" <<'PY'
import hashlib
import struct
import sys
from pathlib import Path

EXPECTED = {
    ("system", 0x01000007): "3b8027881769f9d8498a7c1c888656ff84e068855d7478e8a8516637a837f146",
    ("system", 0x0100000C): "74332e6930e262aa37f6acb69f45fb3ce2a14e48d937259c78e8fbbbde096c13",
    ("img", 0x01000007): "299427631fd59c8a95a4d29d68c3b049cbfd3d018bbde2a7dc03b415302a5f62",
    ("img", 0x0100000C): "ee8c0c75327454d46fe3792fbb0e2f09024f0e29f687be036cfc9f036a7afb2d",
}

def fat_slices(path):
    data = Path(path).read_bytes()
    magic, count = struct.unpack_from(">II", data, 0)
    if magic not in (0xCAFEBABE, 0xCAFEBABF):
        raise ValueError(f"{path}: not a FAT Mach-O")
    fmt = ">iiQQII" if magic == 0xCAFEBABF else ">iiIII"
    size = struct.calcsize(fmt)
    result = []
    for index in range(count):
        values = struct.unpack_from(fmt, data, 8 + index * size)
        result.append((values[0], values[1], values[4], data[values[2]:values[2] + values[3]]))
    return result

def normalized_prefix(blob):
    if blob[:4] != b"\xcf\xfa\xed\xfe":
        raise ValueError("unsupported Mach-O slice")
    command_count = struct.unpack_from("<I", blob, 16)[0]
    normalized = bytearray(blob)
    cursor = 32
    signature_offset = signature_size = None
    for _ in range(command_count):
        command, command_size = struct.unpack_from("<II", normalized, cursor)
        segment_name = bytes(normalized[cursor + 8:cursor + 24]).rstrip(b"\0")
        if command == 0x19 and segment_name == b"__LINKEDIT":
            normalized[cursor + 32:cursor + 40] = b"\0" * 8
            normalized[cursor + 48:cursor + 56] = b"\0" * 8
        elif command == 0x1D:
            signature_offset, signature_size = struct.unpack_from("<II", normalized, cursor + 8)
            normalized[cursor + 8:cursor + 16] = b"\0" * 8
        cursor += command_size
    if signature_offset is None or signature_offset + signature_size > len(blob):
        raise ValueError("invalid or absent LC_CODE_SIGNATURE")
    return bytes(normalized[:signature_offset]), signature_offset

def compare(label, original, rebuilt):
    originals, rebuilts = fat_slices(original), fat_slices(rebuilt)
    if len(originals) != len(rebuilts):
        raise ValueError(f"{label}: architecture count differs")
    for left, right in zip(originals, rebuilts):
        if left[:3] != right[:3]:
            raise ValueError(f"{label}: FAT metadata differs")
        left_prefix, left_offset = normalized_prefix(left[3])
        right_prefix, right_offset = normalized_prefix(right[3])
        if left_offset != right_offset or left_prefix != right_prefix:
            raise ValueError(f"{label}: non-signature Mach-O bytes differ")
        digest = hashlib.sha256(left_prefix).hexdigest()
        if digest != EXPECTED[(label, left[0])]:
            raise ValueError(f"{label}: unexpected normalized digest {digest}")

compare("system", sys.argv[1], sys.argv[2])
compare("img", sys.argv[3], sys.argv[4])
print("framework copies equal the pinned UTM binaries after signature normalization")
PY

for artifact in qemu-system-aarch64 qemu-img qemu-aarch64-softmmu qemu-img-framework; do
  mv -f "$staging/build-1/$artifact" "$output/$artifact"
done

echo "published reproducible candidate-17 UTM QEMU tools in $output"
