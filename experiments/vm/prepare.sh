#!/usr/bin/env bash
set -euo pipefail
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
state="$root/state"
mkdir -p "$state"
usage() {
  echo "usage: $0 --image FILE --kernel FILE --initrd FILE [--payload FILE] [--data FILE] [--data-size SIZE]" >&2
  exit 2
}
image= kernel= initrd= payload="$root/assets/guest-payload.sh" data="$state/data.raw" data_size=64M
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

# Inputs must resolve to regular files below the controlled asset root. The
# persistent data disk is the sole state-root exception.
asset_root="$root/assets"
state_root="$root/state"
validate_asset() {
  local candidate=$1 resolved
  case "/$candidate/" in */../*|*/./*) echo "blocked: path traversal in asset: $candidate" >&2; exit 3;; esac
  [[ "$candidate" = /* ]] || candidate="$root/$candidate"
  [[ -e "$candidate" && ! -L "$candidate" ]] || { echo "blocked: regular non-symlink asset required: $candidate" >&2; exit 3; }
  local parent
  parent=$(dirname -- "$candidate")
  while [[ "$parent" != "/" && "$parent" != "." ]]; do
    [[ ! -L "$parent" ]] || { echo "blocked: symlink in asset path: $parent" >&2; exit 3; }
    parent=$(dirname -- "$parent")
  done
  resolved=$(CDPATH= cd -P -- "$(dirname -- "$candidate")" && pwd)/$(basename -- "$candidate")
  [[ "$resolved" == "$asset_root"/* ]] || { echo "blocked: asset must be below $asset_root: $candidate" >&2; exit 3; }
  [[ -f "$resolved" && ! -L "$resolved" ]] || { echo "blocked: regular non-symlink asset required: $candidate" >&2; exit 3; }
  printf '%s\n' "$resolved"
}
image=$(validate_asset "$image")
kernel=$(validate_asset "$kernel")
initrd=$(validate_asset "$initrd")
payload=$(validate_asset "$payload")
case "/$data/" in */../*|*/./*) echo "blocked: path traversal in data disk: $data" >&2; exit 3;; esac
case "$data" in /*) ;; *) data="$root/$data";; esac
[[ ! -L "$data" ]] || { echo "blocked: data disk must not be a symlink: $data" >&2; exit 3; }
parent=$(dirname -- "$data")
while [[ "$parent" != "/" && "$parent" != "." ]]; do
  [[ ! -L "$parent" ]] || { echo "blocked: symlink in data path: $parent" >&2; exit 3; }
  parent=$(dirname -- "$parent")
done
data=$(CDPATH= cd -P -- "$(dirname -- "$data")" 2>/dev/null && pwd)/$(basename -- "$data")
[[ "$data" == "$state_root"/* ]] || { echo "blocked: data disk must be below $state_root" >&2; exit 3; }
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
python3 - "$root/assets/asset-manifest.json" "$root" "$image" "$kernel" "$initrd" "$payload" "$data" "$data_size" "$qemu_img" <<'PY'
import hashlib, json, pathlib, subprocess, sys
manifest, root, *raw = sys.argv[1:]
manifest, root = pathlib.Path(manifest), pathlib.Path(root).resolve()
asset_root = root / "assets"
state_root = root / "state"
paths = dict(zip(("image", "kernel", "initrd", "payload", "data"), map(pathlib.Path, raw[:5])))
old = {}
if manifest.exists():
    try:
        old = json.loads(manifest.read_text()).get("assets", {})
    except (OSError, ValueError, json.JSONDecodeError):
        raise SystemExit("blocked: invalid existing asset manifest")
items = {}
seen = set()
for name, path in paths.items():
    path = path.resolve(strict=True)
    if path.parent == asset_root and path.name == "asset-manifest.json":
        raise SystemExit("blocked: manifest cannot be an input asset")
    try:
        rel = path.relative_to(root).as_posix()
        if name == "data":
            path.relative_to(state_root)
        else:
            path.relative_to(asset_root)
    except ValueError:
        raise SystemExit(f"blocked: asset outside controlled root: {path}")
    if rel in seen:
        raise SystemExit(f"blocked: asset path collision: {rel}")
    seen.add(rel)
    prior = old.get(name, {})
    if name == "data":
        source_url, version, arch, generation = "generated://qemu-img", "qemu-img", "host", f"{sys.argv[9]} create -f raw {rel} {sys.argv[8]}"
    else:
        source_url, version = prior.get("sourceUrl"), prior.get("version")
        arch, generation = prior.get("architecture"), prior.get("generationCommand")
        if not source_url or not version or not arch or not generation:
            raise SystemExit(f"blocked: missing sourceUrl/version/architecture/generationCommand for {name}")
    suffix = path.name.lower()
    fmt = ("tar.gz" if suffix.endswith(".tar.gz") else "cpio.gz" if suffix.endswith(".gz") else path.suffix.lstrip(".") or "raw")
    items[name] = {"path": rel, "sourceUrl": source_url, "version": version, "architecture": arch, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "sha256Scope": "startup-initial" if name == "data" else "artifact", "format": fmt, "bytes": path.stat().st_size, "generationCommand": generation}
obj = {"schemaVersion": "hb-02.vm-assets.v2", "generatedBy": "experiments/vm/prepare.sh", "generationCommand": "experiments/vm/prepare.sh", "assets": items}
manifest.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
print(json.dumps(obj, indent=2, sort_keys=True))
PY
