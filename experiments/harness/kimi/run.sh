#!/usr/bin/env bash
set -euo pipefail
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
output=${1:-"$root/kimi-probe.json"}
exec python3 "$root/probe.py" --output "$output"
