#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
utm_app=${1:-/Applications/UTM.app}
output=${2:-"$root/tools"}
frameworks="$utm_app/Contents/Frameworks"
system_framework="$frameworks/qemu-aarch64-softmmu.framework/Versions/A/qemu-aarch64-softmmu"
image_framework="$frameworks/qemu-img.framework/Versions/A/qemu-img"
host_major=$(sw_vers -productVersion | cut -d. -f1)
default_sdk=/Library/Developer/CommandLineTools/SDKs/MacOSX${host_major}.sdk
sdk=${HB02_MACOS_SDK:-$default_sdk}

[[ -d "$frameworks" ]] || { echo "blocked: UTM frameworks not found: $frameworks" >&2; exit 3; }
[[ -d "$sdk" ]] || { echo "blocked: macOS SDK not found: $sdk (set HB02_MACOS_SDK)" >&2; exit 3; }
mkdir -p "$output"
staging=$(mktemp -d "$output/.utm-launchers.XXXXXX")
trap 'rm -rf "$staging"' EXIT
clang -O2 -Wall -Wextra -Werror -isysroot "$sdk" "$root/utm-qemu-launcher.c" -o "$staging/qemu-system-aarch64" \
    -Wl,-rpath,"$frameworks"
cp "$staging/qemu-system-aarch64" "$staging/qemu-img"
codesign --force --sign - --entitlements "$root/utm-qemu.entitlements" "$staging/qemu-system-aarch64"
codesign --force --sign - "$staging/qemu-img"
cp "$system_framework" "$staging/qemu-aarch64-softmmu"
cp "$image_framework" "$staging/qemu-img-framework"
codesign --force --sign - "$staging/qemu-aarch64-softmmu"
codesign --force --sign - "$staging/qemu-img-framework"
chmod 0555 "$staging"/*
for artifact in qemu-system-aarch64 qemu-img qemu-aarch64-softmmu qemu-img-framework; do
    mv -f "$staging/$artifact" "$output/$artifact"
done

echo "built UTM QEMU launchers in $output"
echo "export HB02_QEMU_SYSTEM_FRAMEWORK=$output/qemu-aarch64-softmmu"
echo "export HB02_QEMU_IMG_FRAMEWORK=$output/qemu-img-framework"
