#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
exec python3 "$ROOT/experiments/harness/pi/probe.py" "$@"
