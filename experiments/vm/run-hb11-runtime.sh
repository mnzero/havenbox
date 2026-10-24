#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
usage() {
  echo "usage: $0 IMAGE_DIR normal|lease-expiry" >&2
  exit 2
}

image=${1:-}
mode=${2:-}
[[ -d "$image" && ("$mode" == normal || "$mode" == lease-expiry) ]] || usage
image=$(CDPATH= cd -- "$image" && pwd)
runtime_home=$(python3 -c 'from pathlib import Path; print(Path.home())')
helper="$root/.build/engineering/havenbox-helper"
driver="$root/.build/engineering/runtime-integration"
qemu="$root/experiments/vm/tools/qemu-system-aarch64"
qemu_framework="$root/experiments/vm/tools/qemu-aarch64-softmmu"

"$root/experiments/vm/replay-hb02-local.sh" verify >/dev/null
for executable in "$qemu" "$qemu_framework" "$helper" "$driver"; do
  [[ -f "$executable" && ! -L "$executable" && -x "$executable" ]] || {
    echo "blocked: missing executable $executable" >&2
    exit 3
  }
done
python3 - "$image" <<'PY'
import hashlib, json, pathlib, sys
directory = pathlib.Path(sys.argv[1])
manifest = json.loads((directory / "manifest.json").read_text())
for name in ("Image", "initramfs-hb11", "root.raw", "data.raw"):
    path = directory / name
    expected = manifest["files"][name]
    if path.stat().st_size != expected["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
        raise SystemExit(f"blocked: candidate manifest mismatch: {name}")
PY

for asset in "$image/Image" "$image/initramfs-hb11" "$image/root.raw" "$image/data.raw"; do
  [[ $(stat -f '%Lp' "$asset") == 444 ]] || {
    echo "blocked: immutable candidate asset must be mode 0444: $asset" >&2
    exit 3
  }
done

runtime_directory=$(mktemp -d "$runtime_home/hb11-runtime.XXXXXX")
chmod 0700 "$runtime_directory"
cp -c "$image/data.raw" "$runtime_directory/data.raw"
chmod 0600 "$runtime_directory/data.raw"

hash() { shasum -a 256 "$1" | awk '{print $1}'; }
echo "runtime_directory=$runtime_directory"
"$driver" \
  "$qemu" "$(hash "$qemu")" \
  "$qemu_framework" "$(hash "$qemu_framework")" \
  "$image/Image" "$(hash "$image/Image")" \
  "$image/initramfs-hb11" "$(hash "$image/initramfs-hb11")" \
  "$image/root.raw" "$(hash "$image/root.raw")" \
  "$runtime_directory/data.raw" \
  "$helper" "$(hash "$helper")" \
  "$runtime_directory" "$mode"
