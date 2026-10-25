#!/usr/bin/env python3
"""HB-03 local process lifecycle probe.

This is an OS-mechanism probe, not a product-runtime test.  It records full
PID/PPID/PGID snapshots and uses a separate watcher to model the cleanup owner
when the helper dies.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SELF = Path(__file__).resolve()


def ps_snapshot() -> list[dict[str, object]]:
    command = ["/bin/ps", "-axo", "pid=,ppid=,pgid=,stat=,command="]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    rows = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 4)
        if len(fields) == 5:
            pid, ppid, pgid, stat, cmd = fields
            rows.append({"pid": int(pid), "ppid": int(ppid), "pgid": int(pgid), "stat": stat, "command": cmd})
    return rows


def probe_pids() -> set[int]:
    return {int(row["pid"]) for row in ps_snapshot() if str(SELF) in str(row["command"]) and "--probe" not in str(row["command"])}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False).stdout.strip()
    return bool(state) and not state.startswith("Z")


def reap(process: subprocess.Popen[object]) -> None:
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)


def wait_gone(pids: set[int], timeout: float = 4) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(alive(pid) for pid in pids):
        time.sleep(0.05)
    return not any(alive(pid) for pid in pids)


def child() -> int:
    # A real descendant that remains in the helper's process group.
    grandchild = subprocess.Popen([sys.executable, __file__, "--grandchild"])
    try:
        time.sleep(600)
    finally:
        if alive(grandchild.pid):
            grandchild.kill()
        reap(grandchild)
    return 0


def grandchild() -> int:
    time.sleep(600)
    return 0


def kill_group_members(pgid: int, exclude: set[int]) -> None:
    for row in ps_snapshot():
        pid = int(row["pid"])
        if int(row["pgid"]) == pgid and pid not in exclude:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def write_process_info(path: Path, process: subprocess.Popen[object]) -> int:
    pgid = os.getpgid(process.pid)
    path.write_text(json.dumps({"pid": process.pid, "pgid": pgid}))
    return pgid


def read_process_info(path: Path) -> tuple[int, int] | None:
    try:
        info = json.loads(path.read_text())
        return int(info["pid"]), int(info["pgid"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def cleanup_group(pgid: int) -> None:
    if pgid <= 0:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)
    for _ in range(20):
        if not any(int(row["pgid"]) == pgid for row in ps_snapshot()):
            return
        kill_group_members(pgid, set())
        time.sleep(0.02)


def helper(child_info_file: Path, lease_file: Path, ui_pid: int) -> int:
    process = subprocess.Popen([sys.executable, __file__, "--child"], start_new_session=True)
    # Save the process group while the child is definitely alive.  The helper
    # watcher needs this after the helper itself has been killed.
    child_pgid = write_process_info(child_info_file, process)
    try:
        while True:
            if not alive(ui_pid):
                break
            try:
                lease_until = float(lease_file.read_text())
            except (FileNotFoundError, ValueError):
                lease_until = 0
            if time.time() > lease_until:
                break
            time.sleep(0.05)
    finally:
        if alive(process.pid):
            process.kill()
        reap(process)
        cleanup_group(child_pgid)
    return 0


def cleanup_watcher(helper_pid: int, helper_pgid: int, child_info_file: Path) -> int:
    """Independent cleanup owner: waits for helper death, then kills its groups."""
    while alive(helper_pid):
        time.sleep(0.02)
    # Read the child group saved before termination; after helper death, a
    # post-mortem getpgid() lookup is too late and returns ESRCH.
    child_info = read_process_info(child_info_file)
    groups = {helper_pgid}
    if child_info is not None:
        groups.add(child_info[1])
    # Only groups recorded by this invocation are owned by this watcher.
    for pgid in groups:
        cleanup_group(pgid)
    return 0


def kill_process(process: subprocess.Popen[object]) -> None:
    if process.poll() is None:
        process.kill()
    reap(process)


def scenario(name: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="havenbox-hb03-") as directory:
        root = Path(directory)
        lease = root / "lease"
        child_pid_file = root / "child.pid"
        lease.write_text(str(time.time() + 30))
        ui = subprocess.Popen([sys.executable, str(SELF), "--child"], start_new_session=True)
        helper_process = subprocess.Popen([sys.executable, str(SELF), "--helper", str(child_pid_file), str(lease), str(ui.pid)], start_new_session=True)
        watcher = None
        ui_pgid = ui.pid
        child_pgid = None
        try:
            for _ in range(100):
                if child_pid_file.exists():
                    break
                time.sleep(0.05)
            child_info = read_process_info(child_pid_file)
            if child_info is None:
                raise RuntimeError("helper did not save child process-group metadata")
            child_pid, child_pgid = child_info
            ui_pgid = os.getpgid(ui.pid)
            before = ps_snapshot()
            if name == "ui-kill":
                ui.kill()
                reap(ui)
            elif name == "helper-kill":
                helper_pgid = os.getpgid(helper_process.pid)
                watcher = subprocess.Popen([sys.executable, str(SELF), "--watch", str(helper_process.pid), str(helper_pgid), str(child_pid_file)])
                helper_process.kill()
                reap(helper_process)
                watcher.wait(timeout=8)
            elif name == "lease-expiry":
                # Parent only expires the lease; helper must detect and exit naturally.
                lease.write_text(str(time.time() - 1))
                helper_process.wait(timeout=8)
            else:
                raise ValueError(name)
            # Observe the mechanism before test teardown; reap() kills on timeout
            # and would otherwise turn a broken lease/UI watcher into a pass.
            helper_process.wait(timeout=8)
            kill_process(ui)
            cleanup_group(ui_pgid)
            tracked = {ui.pid, helper_process.pid, child_pid}
            wait_gone(tracked)
            # Allow the group cleanup retry loop to settle before taking the
            # post-scenario snapshot; final_probe_pids remains the hard gate.
            time.sleep(0.1)
            after = ps_snapshot()
            matching_after = [
                row
                for row in after
                if int(row["pid"]) in tracked
                or int(row["ppid"]) in tracked
                or int(row["pgid"]) in {child_pgid, os.getpgid(ui.pid) if alive(ui.pid) else -1, os.getpgid(helper_process.pid) if alive(helper_process.pid) else -1}
                # Do not count or signal another concurrent probe's children.
            ]
            return {"scenario": name, "before": before, "after": after, "tracked_after": matching_after, "cleaned": wait_gone(tracked) and not matching_after}
        finally:
            kill_process(helper_process)
            kill_process(ui)
            cleanup_group(ui_pgid)
            info = read_process_info(child_pid_file)
            if info is not None:
                cleanup_group(info[1])
            if watcher is not None:
                kill_process(watcher)


def launchd_check() -> dict[str, object]:
    available = Path('/bin/launchctl').is_file()
    if not available:
        return {"status": "blocked", "launchctl_available": False, "job_registered": False, "job_unload_verified": False, "no_auto_restart_verified": False, "reason": "launchctl unavailable"}
    return {"status": "blocked", "launchctl_available": True, "path": "/bin/launchctl", "job_registered": False, "job_unload_verified": False, "no_auto_restart_verified": False, "reason": "mechanism probe does not register a job; run HB11 integration for actual launchd evidence"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--grandchild", action="store_true")
    parser.add_argument("--helper", nargs=3)
    parser.add_argument("--watch", nargs=3)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    if args.child:
        return child()
    if args.grandchild:
        return grandchild()
    if args.helper:
        return helper(Path(args.helper[0]), Path(args.helper[1]), int(args.helper[2]))
    if args.watch:
        return cleanup_watcher(int(args.watch[0]), int(args.watch[1]), Path(args.watch[2]))
    if not args.probe:
        parser.error("use --probe")
    # Never kill matching historical processes: they may belong to another run.
    cleanup = {"status": "not-performed", "observed_before": sorted(probe_pids() - {os.getpid()}), "remaining_after": []}
    scenarios = [scenario(name) for name in ("ui-kill", "helper-kill", "lease-expiry")]
    final_probe_pids = sorted({int(row["pid"]) for item in scenarios for row in item["tracked_after"]})
    result = {"historical_cleanup": cleanup, "launchd": launchd_check(), "scenarios": scenarios, "final_probe_pids": final_probe_pids}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not cleanup["remaining_after"] and not final_probe_pids and all(item["cleaned"] for item in scenarios) else 1


if __name__ == "__main__":
    raise SystemExit(main())
