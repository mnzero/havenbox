#!/usr/bin/env python3
"""VM lifecycle with verified process identity and persistent asset safety."""
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
PIDFILE = STATE / "qemu.pid"


def identity(pid):
    result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "command="], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def read_record():
    if PIDFILE.is_symlink():
        raise ValueError("refusing symlink pidfile")
    try:
        record = json.loads(PIDFILE.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid or unreadable pidfile") from exc
    if (not isinstance(record, dict) or type(record.get("pid")) is not int
            or record["pid"] <= 1 or not isinstance(record.get("token"), str)
            or len(record["token"]) != 32 or not record.get("identity")
            or not record.get("started_at")):
        raise ValueError("invalid or legacy pidfile")
    return record


def matches(record):
    current = identity(record["pid"])
    try:
        group = os.getpgid(record["pid"])
    except ProcessLookupError:
        return False
    return (current == record["identity"] and record["started_at"] == current.split("  ", 1)[0]
            and group == record["pid"])


def terminate_group(pid):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    time.sleep(0.25)
    # A sandboxed QEMU descendant can reject redundant SIGKILL after the
    # process-group-wide SIGTERM has already taken effect.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)


def stop():
    if not PIDFILE.exists() and not PIDFILE.is_symlink():
        print("stopped: no pidfile")
        return 0
    try:
        record = read_record()
        if not matches(record):
            raise ValueError("stale or reused PID; refusing to signal")
    except (ValueError, OSError, KeyError) as exc:
        PIDFILE.unlink(missing_ok=True)
        print(f"blocked: {exc}; removed stale pidfile", file=sys.stderr)
        return 3
    terminate_group(record["pid"])
    PIDFILE.unlink(missing_ok=True)
    print("stopped verified VM process group")
    return 0


def start(args):
    if PIDFILE.exists() or PIDFILE.is_symlink():
        print("blocked: existing pidfile; run stop.sh first", file=sys.stderr)
        return 3
    timeout = float(os.environ.get("HB02_START_TIMEOUT", "30"))
    if not 0 < timeout <= 300:
        raise ValueError("startup timeout must be in (0, 300] seconds")
    token = uuid.uuid4().hex
    logpath = STATE / "qemu.log"
    if logpath.is_symlink():
        raise ValueError("refusing symlink log")
    with logpath.open("w") as log:
        child = subprocess.Popen([str(ROOT / "run.sh"), "launch", *args, "--instance-token", token], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    success = False
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                print(f"blocked: QEMU launch exited {child.returncode}; see {logpath}", file=sys.stderr)
                return 3
            if f"HB02_READY {token}" in logpath.read_text(errors="replace").splitlines():
                current = identity(child.pid)
                if token in current and child.poll() is None:
                    record = {"pid": child.pid, "token": token, "identity": current, "started_at": current.split("  ", 1)[0]}
                    with PIDFILE.open("x") as f:
                        json.dump(record, f)
                    success = True
                    print(f"started verified QEMU pid={child.pid}; guest handshake received")
                    return 0
            time.sleep(0.05)
        print(f"blocked: guest handshake timed out; see {logpath}", file=sys.stderr)
        return 3
    finally:
        if not success:
            terminate_group(child.pid)
            child.wait()


def main():
    if STATE.is_symlink():
        raise ValueError("refusing symlink state directory")
    STATE.mkdir(exist_ok=True)
    import fcntl
    fd = os.open(STATE / "lifecycle.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        action = sys.argv[1]
        result = start(sys.argv[2:]) if action == "start" else stop()
        if action == "clean" and result == 0:
            (STATE / "qemu.log").unlink(missing_ok=True)
            print("cleaned runtime log; persistent data and assets retained")
        return result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc
