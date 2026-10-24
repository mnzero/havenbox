#!/usr/bin/env python3
"""Fail-closed HB-04 metadata probe; no ACP request adapter is implemented."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path

# Reviewed deployment location; never resolve through PATH or environment input.
EXECUTABLE = "/usr/local/bin/kimi"
COMMANDS = {"cli": (EXECUTABLE, "--version"),
            "acp": (EXECUTABLE, "acp", "--version")}
CAPTURE_LIMIT = 16384  # combined raw stdout + stderr, not just displayed text
EVENTS = ("session", "stream", "tool_rejection", "stop", "error")
OVERRIDES = ("KIMI_CLI", "KIMI_ACP", "KIMI_CODE_HOME")


def redact(value: str | bytes) -> str:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    # Also remove known secrets even when a tool prints only their bare value.
    for key, secret in os.environ.items():
        if secret and re.search(r"key|token|secret|password|credential", key, re.I):
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r'''(?ix)(["']?(?:[\w-]*(?:api[_-]?key|token|secret|password|credential|authorization))["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)''',
                  r"\1[REDACTED]", text)
    return text[:CAPTURE_LIMIT]


def command_summary(command: tuple[str, ...]) -> dict:
    # Never record or echo an untrusted command string.
    return {"profile": next((k for k, v in COMMANDS.items() if v == command), "rejected"),
            "sha256": hashlib.sha256(json.dumps(command).encode()).hexdigest()}


def private_home(home: Path) -> bool:
    try:
        info = home.lstat()
        return (not home.is_symlink() and home.resolve() == home
                and stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
                and info.st_uid == os.getuid())
    except OSError:
        return False


def run(command: tuple[str, ...], *, home: Path, network_allowed: bool,
        timeout: float = 20) -> dict:
    base = {"command": command_summary(command)}
    if not command:
        return {**base, "status": "blocked", "reason": "empty command"}
    if any(name in os.environ for name in OVERRIDES):
        return {**base, "status": "blocked", "reason": "environment overrides forbidden"}
    if not network_allowed:
        return {**base, "status": "blocked", "reason": "network disabled; no external commands executed"}
    if command not in COMMANDS.values():
        return {**base, "status": "blocked", "reason": "command is not an approved fixed profile"}
    if not private_home(home):
        return {**base, "status": "blocked", "reason": "home is not a private owned directory"}
    # Build from scratch: no PATH/PYTHONPATH/LD_PRELOAD/proxy/auth/config inheritance.
    env = {"HOME": str(home), "KIMI_CODE_HOME": str(home),
           "PATH": "/usr/bin:/bin", "LANG": "C", "TMPDIR": str(home),
           "XDG_CONFIG_HOME": str(home), "XDG_CACHE_HOME": str(home)}
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    status, reason = "unknown", ""
    process = None
    try:
        with selectors.DefaultSelector() as selector:
            process = subprocess.Popen(command, env=env, cwd=home, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, shell=False)
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            deadline = time.monotonic() + timeout
            total = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "command timed out"
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, min(4096, CAPTURE_LIMIT - total))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    captured[key.data].extend(chunk)
                    total += len(chunk)
                    if total >= CAPTURE_LIMIT:
                        reason = "capture limit reached; process group terminated"
                        break
                if reason:
                    break
            if not reason:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
                status = "pass" if process.returncode == 0 else "unknown"
    except subprocess.TimeoutExpired:
        reason = "command timed out"
    except OSError as exc:
        status, reason = "blocked", f"execution failed: {type(exc).__name__}"
    finally:
        if process is not None:
            # Clean up descendants too, including those holding inherited pipes.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            process.stdout.close()
            process.stderr.close()
    return {**base, "status": status, "reason": reason,
            "returncode": process.returncode if process else None,
            **{key: redact(bytes(value)) for key, value in captured.items()}}


def probe(home: Path) -> dict:
    network_allowed = os.environ.get("KIMI_ALLOW_NETWORK") == "1"
    overrides = [name for name in OVERRIDES if name in os.environ]
    result = {
        "probe": "HB-04", "schema": 2,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "network_allowed": network_allowed, "business_data_used": False,
        "home_mode": oct(stat.S_IMODE(home.stat().st_mode)),
        "home_lifecycle": "probe-created private temporary directory; removed on exit",
        "rejected_environment_fields": overrides,
        "versions": {name: run(command, home=home, network_allowed=network_allowed)
                     for name, command in COMMANDS.items()},
        "request_shape": {"probe_prompt": "HB-04 contract probe: reply with exactly READY",
                          "tool_suggestion": "shell command `printf TOOL_REJECT` (data only; never executed)",
                          "stop": "terminate stream after first response event"},
    }
    blocked = overrides or not network_allowed or all(
        item["status"] == "blocked" for item in result["versions"].values())
    result["overall"] = "blocked" if blocked else "unknown"
    reason = ("configuration rejected or execution unavailable; see versions" if blocked else
              "real request adapter not configured; no fabricated evidence")
    result["events"] = {name: {"status": result["overall"], "reason": reason} for name in EVENTS}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("kimi-probe.json"))
    args = parser.parse_args()
    try:
        # Ignore TMPDIR and every caller-supplied home; mkdtemp creates exclusively.
        # Safe shared parent: TemporaryDirectory uses exclusive mkdtemp, never a predictable child.
        with tempfile.TemporaryDirectory(prefix="hb04-kimi-", dir=Path("/tmp").resolve()) as directory:  # noqa: S108
            home = Path(directory)
            home.chmod(0o700)
            if not private_home(home):
                raise PermissionError("private temporary home unavailable")
            result = probe(home)
    except OSError as exc:
        result = {"probe": "HB-04", "overall": "blocked",
                  "reason": f"private probe setup failed: {type(exc).__name__}"}
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload)
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
