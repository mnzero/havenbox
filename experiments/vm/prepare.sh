#!/usr/bin/env bash
set -euo pipefail
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
state="$root/state"
mkdir -p "$state"
usage() {
  echo "usage: $0 --image FILE --kernel FILE --initrd FILE [--payload FILE] [--data FILE] [--data-size SIZE]" >&2
  exit 2
}
image= kernel= initrd= payload="$root/guest-payload.sh" data="$state/data.raw" data_size=64M
while (($#)); do
  case "$1" in
    --image) image=${2:?}; shift 2;;
    --kernel) kernel=${2:?}; shift 2;;
    --initrd) initrd=${2:?}; shift 2;;
    --payload) payload=${2:?}; shift 2;;
    --data) data=${2:?}; shift 2;;
    --data-size) data_size=${2:?}; shift 2;;
    *) usage;;
  esac
done
[[ -n "$image" && -n "$kernel" && -n "$initrd" ]] || usage
qemu_img=${QEMU_IMG:-qemu-img}
command -v "$qemu_img" >/dev/null || { echo "blocked: qemu-img is required (set QEMU_IMG)" >&2; exit 3; }
case "$data_size" in
  *M) data_mib=${data_size%M};;
  *G) data_mib=$((${data_size%G} * 1024));;
  *) echo "blocked: --data-size must use integer MiB/GiB" >&2; exit 3;;
esac
[[ "$data_mib" =~ ^[0-9]+$ ]] && (( data_mib >= 1 && data_mib <= 65536 )) || { echo "blocked: --data-size outside 1M..64G" >&2; exit 3; }

for f in "$image" "$kernel" "$initrd" "$payload"; do
  [[ -f "$f" && ! -L "$f" ]] || { echo "blocked: regular non-symlink required: $f" >&2; exit 3; }
done
case "$data" in /*) ;; *) data="$root/$data";; esac
[[ ! -L "$data" ]] || { echo "blocked: data disk must not be a symlink: $data" >&2; exit 3; }
mkdir -p "$(dirname "$data")"
if [[ ! -e "$data" ]]; then
  "$qemu_img" create -f raw "$data" "$data_size"
else
  info=$("$qemu_img" info --output=json "$data") || { echo "blocked: existing data disk info failed" >&2; exit 3; }
  DATA_INFO="$info" DATA_MIB="$data_mib" python3 - <<'PY'
import json, os, sys
try:
    obj=json.loads(os.environ['DATA_INFO'])
    size=int(obj['virtual-size'])
except (ValueError, KeyError, json.JSONDecodeError):
    raise SystemExit('blocked: invalid existing data disk metadata')
if obj.get('format') != 'raw' or size != int(os.environ['DATA_MIB']) * 1024 * 1024:
    raise SystemExit('blocked: existing data disk must be raw with exact requested capacity')
PY
fi
[[ -f "$data" && ! -L "$data" ]] || { echo "blocked: data disk is not regular: $data" >&2; exit 3; }
python3 - "$root/asset-manifest.json" "$image" "$kernel" "$initrd" "$payload" "$data" <<'PY'
import hashlib, json, pathlib, sys
manifest = pathlib.Path(sys.argv[1])
items = {}
for name, raw in zip(("image", "kernel", "initrd", "payload", "data"), sys.argv[2:]):
    p = pathlib.Path(raw).resolve()
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""): h.update(chunk)
    items[name] = {"path": str(p), "bytes": p.stat().st_size, "sha256": h.hexdigest()}
obj = {"schemaVersion": "hb-02.vm-assets.v1", "generatedBy": "experiments/vm/prepare.sh", "assets": items}
manifest.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
print(json.dumps(obj, indent=2, sort_keys=True))
PY
