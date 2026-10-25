#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image=${1:-}
mode=${2:-}
usage="usage: $0 IMAGE_DIR normal|disconnect|lease-expiry|protocol-error|ack-mismatch|ack-replay|unacked-stop|chunk-boundary|kill|restart"
if [[ -z "$image" ]]; then
  echo "$usage" >&2
  exit 2
fi
case "$mode" in
  normal|disconnect|lease-expiry|protocol-error|ack-mismatch|ack-replay|unacked-stop|chunk-boundary|kill|restart) ;;
  *)
    echo "$usage" >&2
    exit 2
    ;;
esac
[[ -d "$image" ]] || {
  echo "blocked: missing reviewed guest image directory: $image" >&2
  exit 3
}
image=$(CDPATH= cd -- "$image" && pwd)
"$root/experiments/vm/replay-hb02-local.sh" verify >/dev/null
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
runtime_directory=$(mktemp -d "$HOME/mvp02-files.XXXXXX")
chmod 0700 "$runtime_directory"
cp -c "$image/data.raw" "$runtime_directory/data.raw"
chmod 0600 "$runtime_directory/data.raw"
echo "runtime_directory=$runtime_directory"
python3 "$root/experiments/vm/probe-mvp02-files.py" \
  "$image" "$runtime_directory/data.raw" "$root/tests/fixtures/csv/valid-sales.csv" "$mode"
