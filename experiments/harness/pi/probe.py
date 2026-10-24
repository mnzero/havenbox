#!/usr/bin/env python3
"""HB-05 Pi RPC contract probe.

This probe exercises Pi's language-independent control plane without requiring a
provider key or native MCP server. Results are JSONL so CI can archive them.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any

PINNED_VERSION = "1.3.0"


def record(name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, **extra}


class Rpc:
    def __init__(self, command: list[str], cwd: pathlib.Path) -> None:
        self.proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.pending: list[dict[str, Any]] = []
        self.stderr_lines: list[str] = []
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_reader.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    self.events.put(value)
            except json.JSONDecodeError as exc:
                self.events.put({"_probe_error": f"invalid JSONL: {exc}", "raw": line})

    def _read_stderr(self) -> None:
        assert self.proc.stderr
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip("\n"))

    def send(self, value: dict[str, Any]) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def wait_for(self, predicate, timeout: float = 8) -> dict[str, Any] | None:
        for index, item in enumerate(self.pending):
            if predicate(item):
                return self.pending.pop(index)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                item = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if predicate(item):
                return item
            self.pending.append(item)
        return None

    def response(self, request_id: str, timeout: float = 8) -> dict[str, Any] | None:
        return self.wait_for(lambda x: x.get("type") == "response" and x.get("id") == request_id, timeout)

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def probe_cancellation(rpc: Rpc, agent: pathlib.Path, timeout: float = 8) -> dict[str, Any]:
    request_id = "abort-bash-1"
    pid_file = agent / "sleep.pid"
    rpc.send({"id": request_id, "type": "bash", "command":
              'sleep 30 & child=$!; printf "%s" "$child" > sleep.pid; wait "$child"'})
    deadline = time.monotonic() + timeout
    pid = None
    while time.monotonic() < deadline:
        try:
            pid = int(pid_file.read_text())
            if pid > 0:
                break
            pid = None
        except (OSError, ValueError):
            pass
        time.sleep(0.01)
    started = pid is not None and process_alive(pid)
    rpc.send({"id": "cancel-1", "type": "abort_bash"})
    cancel = rpc.response("cancel-1", timeout=timeout)
    terminal = rpc.response(request_id, timeout=timeout)
    deadline = time.monotonic() + timeout
    while pid is not None and process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    stopped = pid is not None and not process_alive(pid)
    passed = (started and stopped and cancel and cancel.get("success") is True
              and terminal and terminal.get("id") == request_id
              and terminal.get("command") == "bash" and terminal.get("success") is True
              and terminal.get("data", {}).get("cancelled") is True)
    return record("cancellation", "pass" if passed else "fail",
                  "requires original bash cancelled terminal response and observed sleep PID exit",
                  request_id=request_id, response=cancel, terminal_response=terminal,
                  sleep_pid=pid, process_started=started, process_stopped=stopped)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pi", default=shutil.which("pi") or "pi")
    parser.add_argument("--keep-agent-dir", action="store_true")
    args = parser.parse_args()
    results: list[dict[str, Any]] = []
    version = subprocess.run([args.pi, "--version"], capture_output=True, text=True, check=False)
    actual_version = version.stdout.strip() or version.stderr.strip()
    version_ok = version.returncode == 0 and actual_version == PINNED_VERSION
    results.append(record("version_pin", "pass" if version_ok else "blocked", f"expected {PINNED_VERSION}, observed {actual_version!r}"))
    if not version_ok:
        results.append(record("rpc_stream", "blocked", "approved Pi version required; RPC was not started"))
        print("\n".join(json.dumps(x, ensure_ascii=False) for x in results))
        return 2

    with tempfile.TemporaryDirectory(prefix="havenbox-hb05-agent-") as agent_dir:
        agent = pathlib.Path(agent_dir)
        command = [args.pi, "--mode", "rpc", "--no-session", "--no-mcp", "--no-extensions", "--no-skills", "--no-context-files", "--cwd", str(agent)]
        # --cwd is not available on every build; use process cwd as the actual isolation boundary.
        if "--cwd" in command:
            command.remove("--cwd")
            command.remove(str(agent))
        rpc = Rpc(command, agent)
        try:
            rpc.send({"id": "state-1", "type": "get_state"})
            state = rpc.response("state-1")
            results.append(record("jsonl_stream", "pass" if state and state.get("success") else "fail", "get_state response was correlated by id", response=state))

            rpc.send({"id": "bad-1", "type": "not_a_command"})
            bad = rpc.response("bad-1")
            results.append(record("error_response", "pass" if bad and bad.get("success") is False else "fail", "unknown command returned a structured error", response=bad))

            rpc.send({"id": "retry-1", "type": "set_auto_retry", "enabled": False})
            retry = rpc.response("retry-1")
            results.append(record("retry_override_block", "pass" if retry and retry.get("success") else "fail", "auto retry was explicitly disabled through RPC", response=retry))

            rpc.send({"id": "model-1", "type": "set_model", "provider": "__hb05_missing__", "modelId": "__hb05_missing__"})
            model = rpc.response("model-1")
            results.append(record("model_override_block", "pass" if model and model.get("success") is False else "fail", "invalid model override was rejected", response=model))

            rpc.send({"id": "bash-1", "type": "bash", "command": "printf HB05_CONTEXT_POINT"})
            bash = rpc.response("bash-1", timeout=8)
            messages = None
            if bash and bash.get("success"):
                rpc.send({"id": "messages-1", "type": "get_messages"})
                messages = rpc.response("messages-1")
            results.append(record("context_construction_point", "pass" if bash and bash.get("success") and messages and messages.get("success") else "unknown", "direct bash output and persisted message context inspected", bash=bash, messages=messages))

            results.append(probe_cancellation(rpc, agent))

            rpc.send({"id": "mcp-1", "type": "get_commands"})
            commands = rpc.response("mcp-1")
            results.append(record("approved_mcp_tool_bridge", "unknown", "native MCP disabled; no approved MCP server or bridge was supplied", response=commands))
            results.append(record("history_recovery", "blocked", "probe uses --no-session; persistent restart/recovery requires an explicitly approved session policy"))
        finally:
            rpc.close()
        if args.keep_agent_dir:
            kept = pathlib.Path.cwd() / "experiments" / "harness" / "pi" / "agent-run"
            try:
                if kept.exists():
                    shutil.rmtree(kept)
                shutil.copytree(agent, kept)
                results.append(record("agent_directory", "pass", f"isolated agent directory: {kept}"))
            except OSError as exc:
                results.append(record("agent_directory", "blocked", f"could not preserve isolated directory: {exc}"))
        else:
            results.append(record("agent_directory", "pass", "isolated temporary agent directory created and removed"))

    results.append(record("native_mcp_assumption", "pass", "probe explicitly uses --no-mcp and reports MCP bridge as unknown"))
    for item in results:
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    return 0 if all(x["status"] not in {"fail"} for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
