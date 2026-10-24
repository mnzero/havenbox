#!/usr/bin/env python3
"""Small user launchd lifecycle helper for HB-03 verification.

The helper owns a process group, expires an optional lease, and exits once its
tracked UI PID disappears.  It deliberately has no restart policy; launchd
must not relaunch it after a normal or signalled exit.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

_child: subprocess.Popen[bytes] | None = None
_stop = False


def stop(_signum: int, _frame: object) -> None:
    global _stop
    _stop = True


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def terminate_child() -> None:
    global _child
    if _child is None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(_child.pid), signal.SIGTERM)
    try:
        _child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(_child.pid), signal.SIGKILL)
        _child.wait(timeout=2)
    _child = None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-pid", type=int, required=True)
    parser.add_argument("--lease-file", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--child-command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    args.state_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    global _child
    if args.child_command:
        _child = subprocess.Popen(args.child_command, start_new_session=True)
    try:
        while not _stop and alive(args.ui_pid):
            try:
                lease_until = float(args.lease_file.read_text(encoding="utf-8"))
            except (FileNotFoundError, ValueError):
                lease_until = 0
            if time.time() > lease_until:
                break
            time.sleep(0.05)
    finally:
        terminate_child()
        with contextlib.suppress(FileNotFoundError):
            args.state_file.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
