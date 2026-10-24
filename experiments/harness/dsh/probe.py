#!/usr/bin/env python3
"""Fail-closed probe for the pinned dsh runtime.

Only the launcher named by runtime-manifest.json may run. A system `dsh` and
HB06_DSH_BIN are deliberately ignored. A live probe is permitted only when
both pinned artifact hashes are present and match.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "runtime-manifest.json"
COMMIT = "22ddee3792402c3e70ec5f86a63c3e3e895a2f60"
VERSION = "0.2.1-rc.3"
LOCKFILE = "runtime/package-lock.json"
ADAPTER = "hb06-dsh-probe-2"


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rpc_probe(
    binary: Path, env: dict[str, str], cwd: Path, *,
    timeout: float = 5.0, max_output: int = 65536,
) -> tuple[dict[str, object], int | None]:
    """Bound all pipe I/O by one deadline and a combined stdout/stderr limit."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "cwd": str(cwd), "provider": None, "model": None, "approvalRequests": False,
    }}
    pending = (json.dumps(request) + "\n").encode()
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        [str(binary), "--profile", "sdk-minimal"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env,
        start_new_session=True,
    )
    response = bytearray()
    stderr_observed = False
    total = 0
    status = "eof"
    try:
        with selectors.DefaultSelector() as selector:
            for stream, event in ((process.stdin, selectors.EVENT_WRITE),
                                  (process.stdout, selectors.EVENT_READ),
                                  (process.stderr, selectors.EVENT_READ)):
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = "timeout"
                    break
                events = selector.select(remaining)
                if not events:
                    status = "timeout"
                    break
                finished = False
                for key, _ in events:
                    stream = key.fileobj
                    if stream is process.stdin:
                        try:
                            pending = pending[os.write(stream.fileno(), pending):]
                        except BrokenPipeError:
                            pending = b""
                        if not pending:
                            selector.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), min(4096, max_output - total + 1))
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    total += len(chunk)
                    if stream is process.stderr:
                        stderr_observed = True
                    if total > max_output:
                        status, finished = "output_limit", True
                        break
                    if stream is process.stdout:
                        response.extend(chunk)
                        if b"\n" in response:
                            status, finished = "response", True
                            break
                if finished:
                    break
    finally:
        # Kill the session's process group even if the launcher already exited:
        # descendants may still own the pipes. No blocking drain/read follows.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
    parsed: object = None
    if status == "response":
        try:
            parsed = json.loads(response.split(b"\n", 1)[0])
        except (json.JSONDecodeError, UnicodeDecodeError):
            status = "malformed_response"
    return {"initializeResponse": parsed, "stderrObserved": stderr_observed,
            "readStatus": status}, process.returncode


def manifest_issues(manifest: dict[str, object]) -> list[str]:
    expected = {"version": VERSION, "commit": COMMIT, "lockfile": LOCKFILE}
    issues: list[str] = []
    for field, value in expected.items():
        if manifest.get(field) != value:
            issues.append(f"blocked: runtime manifest {field} does not match pinned value")
    return issues


def load_manifest() -> dict[str, object]:
    try:
        value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid runtime manifest: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError("invalid runtime manifest: expected object")
    return value


def create_home() -> tuple[Path, Path, Path]:
    try:
        home = Path(tempfile.mkdtemp(prefix="hb06-dsh-home-"))
        dsh_home = home / "dsh"
        work = home / "workspace"
        dsh_home.mkdir()
        work.mkdir()
        return home, dsh_home, work
    except OSError as error:
        raise RuntimeError(f"cannot create isolated home: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="output", type=Path)
    args = parser.parse_args()
    target = args.output or ROOT / "probe-result.json"
    try:
        manifest = load_manifest()
    except RuntimeError as error:
        record = {
            "candidateId": "dsh", "adapterVersion": ADAPTER,
            "version": None, "sourceUrlOrCommit": None,
            "result": "blocked", "observations": [f"blocked: {error}"],
            "actual": {}, "exitCode": None,
        }
        payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
        except OSError as output_error:
            print(f"probe output failed: {output_error}", file=sys.stderr)
            return 2
        print(payload, end="")
        return 3

    manifest_reasons = manifest_issues(manifest)
    if manifest_reasons:
        record = {
            "candidateId": "dsh", "adapterVersion": ADAPTER,
            "version": manifest.get("version"),
            "sourceUrlOrCommit": f"{manifest.get('source')}@{manifest.get('commit')}",
            "result": "blocked", "observations": manifest_reasons,
            "actual": {}, "exitCode": None,
        }
        payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
        except OSError as output_error:
            print(f"probe output failed: {output_error}", file=sys.stderr)
            return 2
        print(payload, end="")
        return 3

    # Deliberately do not read HB06_DSH_BIN. The manifest is the sole authority.
    binary = ROOT / manifest["launcher"]
    lockfile = ROOT / manifest["lockfile"]
    expected_launcher_hash = manifest.get("launcherSha256")
    expected_lock_hash = manifest.get("lockfileSha256")
    try:
        observed_launcher_hash = sha256(binary)
        observed_lock_hash = sha256(lockfile)
    except OSError as error:
        observed_launcher_hash = None
        observed_lock_hash = None
        reasons: list[str] = [f"blocked: runtime hash inspection failed: {error}"]
    else:
        reasons = []

    try:
        launcher_available = binary.is_file() and os.access(binary, os.X_OK)
        lockfile_available = lockfile.is_file()
    except OSError as error:
        launcher_available = False
        lockfile_available = False
        reasons.append(f"blocked: runtime artifact inspection failed: {error}")
    if not launcher_available:
        reasons.append("blocked: pinned launcher is absent or not executable")
    if not lockfile_available:
        reasons.append("blocked: pinned lockfile is absent")

    if not expected_launcher_hash or not expected_lock_hash:
        reasons.append("blocked: runtime manifest has no approved launcher/lockfile hash")
    if expected_launcher_hash and observed_launcher_hash != expected_launcher_hash:
        reasons.append("blocked: launcher sha256 does not match manifest")
    if expected_lock_hash and observed_lock_hash != expected_lock_hash:
        reasons.append("blocked: lockfile sha256 does not match manifest")

    record: dict[str, object] = {
        "candidateId": "dsh", "adapterVersion": ADAPTER,
        "version": VERSION, "sourceUrlOrCommit": f"{manifest['source']}@{COMMIT}",
        "launcherPath": str(binary), "lockfilePath": str(lockfile),
        "expectedLauncherSha256": expected_launcher_hash, "observedLauncherSha256": observed_launcher_hash,
        "expectedLockfileSha256": expected_lock_hash, "observedLockfileSha256": observed_lock_hash,
        "command": [str(binary), "--profile", "sdk-minimal"],
        "environment": None,
        "result": "blocked" if reasons else "unknown", "observations": reasons,
        "actual": {}, "exitCode": None, "homeCreated": False, "homeRemoved": False,
        "evidence": {"network": "not_started", "telemetry": "disabled-by-env-unverified", "keyPersistence": "sentinel-scan-pending", "approval": "not_started", "cancellation": "not_started", "errors": "not_started", "recovery": "not_started"},
        "unverifiedBoundaries": ["network isolation", "telemetry suppression", "key persistence", "approval", "cancellation", "error", "recovery"],
    }
    # No isolated directories or child environment exist until every pinned
    # version and artifact check above has passed (including both hashes).
    home = None
    if not reasons:
        try:
            home, dsh_home, work = create_home()
        except RuntimeError as error:
            print(f"probe setup failed: {error}", file=sys.stderr)
            return 2
        env = os.environ.copy()
        env.update({
            "DSH_HOME": str(dsh_home), "DSH_PROFILE": "sdk-minimal",
            "DSH_TELEMETRY": "0", "DEEPSEEK_API_KEY": "__HB06_SENTINEL_NEVER_A_REAL_KEY__",
        })
        record["homeCreated"] = True
        record["environment"] = {"DSH_HOME": str(dsh_home), "profile": "sdk-minimal",
                                 "network": "gateway-only", "telemetry": "disabled"}
        try:
            live, code = rpc_probe(binary, env, work)
            record["actual"] = live
            record["exitCode"] = code
            record["result"] = "passed" if live.get("initializeResponse") else "unknown"
            record["observations"] = ["real pinned launcher started", "one initialize JSON-RPC request sent", "process terminated after bounded probe"]
            record["evidence"] = {**record["evidence"], "network": "not_started-by-probe", "keyPersistence": "sentinel-scan-required"}
        except OSError as error:
            record["result"] = "blocked"
            record["observations"] = [f"blocked: pinned launcher could not start: {error}"]
    if home is not None:
        try:
            shutil.rmtree(home)
            record["homeRemoved"] = not home.exists()
        except OSError as error:
            record["homeRemoved"] = False
            record["observations"] = [*record["observations"], f"cleanup failed: {error}"]

    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    except OSError as error:
        print(f"probe output failed: {error}", file=sys.stderr)
        return 2

    print(payload, end="")
    return 0 if record["result"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
