#!/usr/bin/env bash
set -euo pipefail
# HB-02 reproducible entry point. Launch requires real guest artifacts.
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$root/probe.py" "$@"
