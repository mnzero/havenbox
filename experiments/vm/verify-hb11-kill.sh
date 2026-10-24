#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image=${1:-}
target=${2:-}
if [[ ! -d "$image" || ("$target" != ui && "$target" != helper && "$target" != qemu) ]]; then
  echo "usage: $0 IMAGE_DIR ui|helper|qemu" >&2
  exit 2
fi
image=$(CDPATH= cd -- "$image" && pwd)
log=$(mktemp "$HOME/hb11-${target}-kill.XXXXXX")

"$root/experiments/vm/run-hb11-runtime.sh" "$image" normal >"$log" 2>&1 &
runner_pid=$!
cleanup() {
  if kill -0 "$runner_pid" 2>/dev/null; then
    kill -TERM "$runner_pid" 2>/dev/null || true
    wait "$runner_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

deadline=$((SECONDS + 45))
runtime_directory=
while ((SECONDS < deadline)); do
  runtime_directory=$(sed -n 's/^runtime_directory=//p' "$log" | head -1)
  if [[ -n "$runtime_directory" && -f "$runtime_directory/launch.json" ]] && \
     grep -Fx 'stage=heartbeat' "$log" >/dev/null; then
    break
  fi
  kill -0 "$runner_pid" 2>/dev/null || {
    echo "blocked: runtime exited before the kill point; log=$log" >&2
    exit 3
  }
  sleep 0.1
done
[[ -n "$runtime_directory" && -f "$runtime_directory/launch.json" ]] || {
  echo "blocked: runtime did not reach the kill point; log=$log" >&2
  exit 3
}

read -r instance label < <(python3 - "$runtime_directory/launch.json" <<'PY'
import json, sys
instance = json.load(open(sys.argv[1]))["instance"].lower()
print(instance, "dev.havenbox.runtime." + instance)
PY
)
domain="gui/$(id -u)"
driver_pid=$(pgrep -P "$runner_pid" -f "$root/.build/engineering/runtime-integration" || true)
helper_pid=$(launchctl print "$domain/$label" | awk '/^[[:space:]]*pid = / {print $3; exit}')
qemu_pid=$(pgrep -P "$helper_pid" -f "$root/experiments/vm/tools/qemu-system-aarch64" || true)
[[ "$driver_pid" =~ ^[0-9]+$ && "$helper_pid" =~ ^[0-9]+$ && "$qemu_pid" =~ ^[0-9]+$ ]] || {
  echo "blocked: exact runtime process tree was not found; log=$log" >&2
  exit 3
}

case "$target" in
  ui) victim_pid=$driver_pid ;;
  helper) victim_pid=$helper_pid ;;
  qemu) victim_pid=$qemu_pid ;;
esac
kill -KILL "$victim_pid"
set +e
wait "$runner_pid"
runner_exit=$?
set -e

deadline=$((SECONDS + 12))
while ((SECONDS < deadline)); do
  if ! launchctl print "$domain/$label" >/dev/null 2>&1 && \
     ! kill -0 "$helper_pid" 2>/dev/null && ! kill -0 "$qemu_pid" 2>/dev/null; then
    break
  fi
  sleep 0.1
done
if launchctl print "$domain/$label" >/dev/null 2>&1 || \
   kill -0 "$helper_pid" 2>/dev/null || kill -0 "$qemu_pid" 2>/dev/null; then
  echo "failed: runtime process or launchd job remained; log=$log" >&2
  exit 1
fi
for name in launch.json launchd.plist ui-control.fifo control.sock file.sock inference.sock; do
  [[ ! -e "$runtime_directory/$name" ]] || {
    echo "failed: control artifact remained: $runtime_directory/$name" >&2
    exit 1
  }
done

trap - EXIT
echo "kill-target=$target runner-exit=$runner_exit cleanup=true runtime-directory=$runtime_directory log=$log"
