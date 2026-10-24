#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
utm_app=${1:-/Applications/UTM.app}
output=${2:-"$root/tools"}
frameworks="$utm_app/Contents/Frameworks"
host_major=$(sw_vers -productVersion | cut -d. -f1)
default_sdk=/Library/Developer/CommandLineTools/SDKs/MacOSX${host_major}.sdk
sdk=${HB02_MACOS_SDK:-$default_sdk}

[[ -d "$frameworks" ]] || { echo "blocked: UTM frameworks not found: $frameworks" >&2; exit 3; }
[[ -d "$sdk" ]] || { echo "blocked: macOS SDK not found: $sdk (set HB02_MACOS_SDK)" >&2; exit 3; }
mkdir -p "$output"
clang -O2 -Wall -Wextra -Werror -isysroot "$sdk" "$root/utm-qemu-launcher.c" -o "$output/qemu-system-aarch64" \
    -Wl,-rpath,"$frameworks"
cp "$output/qemu-system-aarch64" "$output/qemu-img"
codesign --force --sign - --entitlements "$root/utm-qemu.entitlements" "$output/qemu-system-aarch64"
codesign --force --sign - "$output/qemu-img"

echo "built UTM QEMU launchers in $output"
echo "export HB02_UTM_APP=$utm_app"
