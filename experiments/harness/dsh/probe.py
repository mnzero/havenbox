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
COMMIT = "1f81c047fc48fa190a683e469daa699857e9afe8"
VERSION = "0.2.1-rc.3"
LOCKFILE = "runtime/package-lock.json"
ADAPTER = "hb06-dsh-probe-3"


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
    requests: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], int | None]:
    """Bound all pipe I/O by one deadline and a combined stdout/stderr limit."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "cwd": str(cwd), "provider": "deepseek-official", "model": "deepseek-chat",
    }}
    queue = list(requests) if requests is not None else [request]
    current = queue.pop(0)
    messages: list[object] = []
    pending = (json.dumps(current) + "\n").encode()
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
                        while b"\n" in response:
                            line, _, rest = response.partition(b"\n")
                            response = bytearray(rest)
                            try:
                                message = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                status, finished = "malformed_response", True
                                break
                            messages.append(message)
                            if isinstance(message, dict) and message.get("id") == current["id"]:
                                if queue:
                                    current = queue.pop(0)
                                    pending = (json.dumps(current) + "\n").encode()
                                    selector.register(process.stdin, selectors.EVENT_WRITE)
                                else:
                                    status, finished = "response", True
                                    break
                        if finished:
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
    parsed = next((m for m in messages if isinstance(m, dict) and m.get("id") == 1), None)
    return {"initializeResponse": parsed, "messages": messages,
            "stderrObserved": stderr_observed, "readStatus": status,
            "processGroupKillSent": True}, process.returncode


def tree_sha256(root: Path) -> str | None:
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            digest.update(b"link\0" + name + b"\0" + os.readlink(path).encode() + b"\0")
        elif path.is_file():
            digest.update(b"file\0" + name + b"\0" + sha256(path).encode() + b"\0")
    return digest.hexdigest()


def protocol_requests(work: Path) -> list[dict[str, object]]:
    pairs = [
        ("initialize", {"cwd": str(work), "provider": "deepseek-official", "model": "deepseek-chat"}),
        ("session/working-directory/get", {"sessionId": "hb06-no-inference"}),
        ("session/wait", {"sessionId": "hb06-no-inference"}),
        ("session/wait", {"sessionId": "missing"}),
        ("session/cancel", {"sessionId": "hb06-no-inference"}),
        ("session/resume", {"sessionId": "missing"}),
        ("initialize", {"cwd": str(work), "provider": "hb06-no-adapter", "model": "none"}),
        ("shutdown", {}),
    ]
    return [{"jsonrpc": "2.0", "id": i, "method": method, "params": params}
            for i, (method, params) in enumerate(pairs, 1)]


def protocol_checks(live: dict[str, object], work: Path) -> dict[str, bool]:
    replies = {m.get("id"): m for m in live["messages"] if isinstance(m, dict) and "id" in m}
    return {
        "complete": live["readStatus"] == "response",
        "initialize": replies.get(1, {}).get("result") == {
            "serverInfo": {"name": "deepseek-harness-sdk-runtime", "version": "0.0.1"}},
        "sessionCreated": replies.get(2, {}).get("result") == {"cwd": str(work)},
        "idleWait": replies.get(3, {}).get("result") == {},
        "missingSessionError": "error" in replies.get(4, {}),
        "cancelUnsupported": "error" in replies.get(5, {}),
        "missingResumeError": "error" in replies.get(6, {}),
        "missingAdapterError": "error" in replies.get(7, {}),
        "shutdown": replies.get(8, {}).get("result") == {},
    }


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

    # Lock bytes alone do not prove installed code; verify the actual private tree.
    if not reasons:
        try:
            artifacts = manifest.get("artifacts", {})
            for path, expected in artifacts.items():
                if sha256(ROOT / path) != expected:
                    reasons.append(f"blocked: artifact hash mismatch: {path}")
            if not artifacts or tree_sha256(ROOT / "runtime/node_modules") != manifest.get("installedTreeSha256"):
                reasons.append("blocked: installed dependency tree mismatch or missing approval")
        except OSError as error:
            reasons.append(f"blocked: installed artifact inspection failed: {error}")

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
        "evidence": {"network": "not_started", "telemetry": "profile-omission-only-unverified", "keyPersistence": "sentinel-scan-pending", "approval": "not_started", "cancellation": "not_started", "errors": "not_started", "recovery": "not_started"},
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
        # Do not inherit credentials, NODE_OPTIONS, proxy or user config paths.
        env = {
            "HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(home),
            "DSH_HOME": str(dsh_home),
            "DEEPSEEK_BASE_URL": "http://127.0.0.1:1",
            "DEEPSEEK_API_KEY": "__HB06_SENTINEL_NEVER_A_REAL_KEY__",
        }
        record["homeCreated"] = True
        record["environment"] = {"DSH_HOME": str(dsh_home), "profile": "sdk-minimal",
                                 "network": "host-not-isolated; loopback-only model configuration",
                                 "telemetry": "omitted-by-sdk-minimal-unverified"}
        try:
            live, code = rpc_probe(binary, env, work, timeout=20, requests=protocol_requests(work))
            record["actual"] = live
            record["exitCode"] = code
            record["protocolChecks"] = protocol_checks(live, work)
            record["result"] = "observed" if all(record["protocolChecks"].values()) else "unknown"
            record["observations"] = ["real pinned launcher started", "non-inference SDK sequence sent", "process group killed after bounded probe; not active-turn cancellation evidence"]
            record["evidence"] = {**record["evidence"], "network": "loopback-route-configured-no-inference-sent; not firewall proof", "errors": "missing session/resume/adapter and unsupported cancel observed", "cancellation": "unsupported RPC observed; idle runtime process group killed", "keyPersistence": "sentinel-scan-pending"}
        except OSError as error:
            record["result"] = "blocked"
            record["observations"] = [f"blocked: pinned launcher could not start: {error}"]
    if home is not None:
        sentinel = b"__HB06_SENTINEL_NEVER_A_REAL_KEY__"
        files = [p for p in home.rglob("*") if p.is_file() and not p.is_symlink()]
        record["persistenceScan"] = {"files": len(files), "sentinelFound": any(
            sentinel in p.read_bytes() for p in files)}
        record["evidence"]["keyPersistence"] = "scan of disposable home only; not general credential proof"
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
    return 0 if record["result"] == "observed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
