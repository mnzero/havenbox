#!/usr/bin/env python3
"""HB-02 bounded protocol and fail-closed VM probe."""
from __future__ import annotations

import argparse
import io
import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO

MAX_FRAME = 1024 * 1024
MAX_CHUNK = 256 * 1024
MAX_TRANSFER_ID = 128
MIN_MEMORY_MIB, MAX_MEMORY_MIB = 128, 8192
MIN_DISK_BYTES, MAX_DISK_BYTES = 1024 * 1024, 64 * 1024**3
MAX_IMAGE_BYTES = 16 * 1024**3
KINDS = {"control", "file_chunk", "inference_stream"}
CONTROL = {"hello", "cancel", "ping", "epoch_reset"}


class ProtocolError(ValueError):
    pass


class FrameChannel:
    def __init__(self, epoch: int):
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            raise ProtocolError("invalid epoch")
        self.epoch = epoch
        self.cancelled: set[str] = set()

    def encode(self, kind: str, epoch: int, payload: dict) -> bytes:
        if kind not in KINDS:
            raise ProtocolError("unknown message kind")
        body = {"kind": kind, "epoch": epoch, "payload": payload}
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > MAX_FRAME:
            raise ProtocolError("frame exceeds maximum")
        return struct.pack(">I", len(raw)) + raw

    def _read_exact(self, stream: BinaryIO, size: int, cancel_event: threading.Event | None) -> bytes:
        out = bytearray()
        fd = None
        if hasattr(stream, "fileno"):
            try:
                fd = stream.fileno()
            except (AttributeError, io.UnsupportedOperation):
                fd = None
        while len(out) < size:
            if cancel_event is not None and cancel_event.is_set():
                raise ProtocolError("read cancelled")
            if fd is not None:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if not ready:
                    continue
            part = stream.read(size - len(out))
            if not part:
                break
            out.extend(part)
        return bytes(out)

    def decode(self, stream: BinaryIO, cancel_event: threading.Event | None = None) -> dict:
        prefix = self._read_exact(stream, 4, cancel_event)
        if len(prefix) != 4:
            raise ProtocolError("truncated frame length")
        size = struct.unpack(">I", prefix)[0]
        if size == 0 or size > MAX_FRAME:
            raise ProtocolError("invalid frame length")
        raw = self._read_exact(stream, size, cancel_event)
        if len(raw) != size:
            raise ProtocolError("truncated frame")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolError("invalid JSON") from exc
        if not isinstance(msg, dict) or set(msg) != {"kind", "epoch", "payload"}:
            raise ProtocolError("invalid frame schema")
        if msg["kind"] not in KINDS:
            raise ProtocolError("unknown message kind")
        if msg["epoch"] != self.epoch:
            raise ProtocolError("stale or future epoch")
        if not isinstance(msg["payload"], dict):
            raise ProtocolError("payload must be object")
        if msg["kind"] == "control":
            action = msg["payload"].get("action")
            if action not in CONTROL:
                raise ProtocolError("unknown control action")
            if action == "cancel":
                transfer_id = msg["payload"].get("transfer_id")
                self._validate_transfer_id(transfer_id)
                self.cancel(transfer_id)
        elif msg["kind"] == "file_chunk":
            p = msg["payload"]
            transfer_id = p.get("transfer_id")
            self._validate_transfer_id(transfer_id)
            offset = p.get("offset")
            if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
                raise ProtocolError("invalid file chunk offset")
            data = p.get("data", "")
            if not isinstance(data, str) or len(data.encode()) > MAX_CHUNK:
                raise ProtocolError("file chunk exceeds maximum")
            if transfer_id in self.cancelled:
                raise ProtocolError("cancelled transfer")
        return msg

    @staticmethod
    def _validate_transfer_id(transfer_id: object) -> None:
        if not isinstance(transfer_id, str) or not transfer_id or len(transfer_id) > MAX_TRANSFER_ID:
            raise ProtocolError("invalid transfer_id")

    def cancel(self, transfer_id: str) -> None:
        self._validate_transfer_id(transfer_id)
        self.cancelled.add(transfer_id)


def expect_error(fn, text: str) -> None:
    try:
        fn()
    except ProtocolError as exc:
        assert text in str(exc), (str(exc), text)
    else:
        raise AssertionError(f"expected ProtocolError: {text}")


def self_test() -> int:
    ch = FrameChannel(7)
    valid = ch.encode("control", 7, {"action": "ping"})
    assert ch.decode(io.BytesIO(valid))["payload"]["action"] == "ping"
    expect_error(lambda: ch.decode(io.BytesIO(struct.pack(">I", MAX_FRAME + 1))), "invalid frame")
    expect_error(lambda: ch.decode(io.BytesIO(ch.encode("control", 6, {"action": "ping"}))), "epoch")
    expect_error(lambda: ch.decode(io.BytesIO(struct.pack(">I", 3) + b"x")), "truncated frame")
    expect_error(lambda: ch.decode(io.BytesIO(ch.encode("control", 7, {"action": "unknown"}))), "unknown control")
    unknown = json.dumps({"kind": "unknown", "epoch": 7, "payload": {}}).encode()
    expect_error(lambda: ch.decode(io.BytesIO(struct.pack(">I", len(unknown)) + unknown)), "unknown")
    huge = {"kind": "file_chunk", "epoch": 7, "payload": {"transfer_id": "t", "offset": 0, "data": "x" * (MAX_CHUNK + 1)}}
    raw = json.dumps(huge).encode()
    expect_error(lambda: ch.decode(io.BytesIO(struct.pack(">I", len(raw)) + raw)), "exceeds")
    for offset in (-1, True):
        expect_error(lambda offset=offset: ch.decode(io.BytesIO(ch.encode("file_chunk", 7, {"transfer_id": "t2", "offset": offset, "data": "x"}))), "offset")
    expect_error(lambda: ch.decode(io.BytesIO(ch.encode("file_chunk", 7, {"transfer_id": "", "offset": 0, "data": "x"}))), "transfer_id")
    cancel = ch.encode("control", 7, {"action": "cancel", "transfer_id": "t"})
    ch.decode(io.BytesIO(cancel))
    cancelled = ch.encode("file_chunk", 7, {"transfer_id": "t", "offset": 0, "data": "x"})
    expect_error(lambda: ch.decode(io.BytesIO(cancelled)), "cancelled")

    read_fd, write_fd = os.pipe()
    cancel_event = threading.Event()
    outcome: list[str] = []
    def blocked_read() -> None:
        try:
            ch.decode(os.fdopen(read_fd, "rb", closefd=True), cancel_event)
        except ProtocolError as exc:
            outcome.append(str(exc))
    thread = threading.Thread(target=blocked_read)
    thread.start()
    time.sleep(0.1)
    cancel_event.set()
    thread.join(1)
    os.close(write_fd)
    assert not thread.is_alive() and outcome == ["read cancelled"], (thread.is_alive(), outcome)
    print(json.dumps({"result": "passed", "scope": "frame-channel-and-cancellable-pipe", "vm_boundary": "unverified"}))
    return 0


def regular_file(path: Path, label: str, max_bytes: int) -> int:
    st = path.lstat()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular non-symlink file")
    if st.st_size <= 0 or st.st_size > max_bytes:
        raise ValueError(f"{label} size outside limit")
    return st.st_size


def qemu_command(qemu: str, image: Path, data: Path, payload: Path, kernel: Path, initrd: Path, memory: str, cpus: int) -> list[str]:
    return [qemu, "-M", "virt", "-accel", "hvf", "-cpu", "host", "-smp", str(cpus), "-m", memory,
            "-nodefaults", "-nographic", "-serial", "stdio", "-kernel", str(kernel), "-initrd", str(initrd),
            "-append", "console=ttyAMA0", "-fw_cfg", f"name=opt/havenbox.payload,file={payload}",
            "-drive", f"if=virtio,format=raw,file={image},readonly=on", "-drive", f"if=virtio,format=raw,file={data},snapshot=off"]


def await_guest_ready(proc: subprocess.Popen, token: str, timeout: float) -> None:
    """Accept only a complete nonce response read from QEMU's serial stdout."""
    deadline = time.monotonic() + timeout
    pending = bytearray()
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise ValueError(f"QEMU exited before readiness: {proc.returncode}")
        readable, _, _ = select.select([proc.stdout], [], [], 0.05)
        if not readable:
            continue
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            raise ValueError("QEMU serial closed before readiness")
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, remaining = pending.partition(b"\n")
            pending = bytearray(remaining)
            line = line.rstrip(b"\r")
            # Prefix serial data so it can never impersonate the host readiness record.
            print(f"guest: {line.decode(errors='replace')}", flush=True)
            if line.startswith(b"HB02_GUEST_READY"):
                if line != f"HB02_GUEST_READY {token}".encode():
                    raise ValueError("invalid guest handshake")
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    return
                raise ValueError(f"QEMU exited during readiness: {proc.returncode}")
        if len(pending) > MAX_FRAME:
            raise ValueError("guest serial line exceeds maximum")
    raise ValueError("guest handshake timed out")


def launch(args: argparse.Namespace) -> int:
    qemu = args.qemu or "qemu-system-aarch64"
    qemu_img = args.qemu_img or "qemu-img"
    image, data, payload, kernel, initrd = map(Path, (args.image, args.data, args.payload, args.kernel, args.initrd))
    reasons: list[str] = []
    try:
        regular_file(image, "image", MAX_IMAGE_BYTES)
        data_bytes = regular_file(data, "data disk", MAX_DISK_BYTES)
        if data_bytes < MIN_DISK_BYTES:
            reasons.append("data disk below minimum capacity")
        if image.suffix.lower() not in {".raw", ".img"}:
            reasons.append("image filename must use raw-compatible .raw or .img suffix")
        if data.suffix.lower() not in {".raw", ".img"}:
            reasons.append("data disk filename must use raw-compatible .raw or .img suffix")
        if image.resolve() == data.resolve():
            reasons.append("image and data disk must be distinct files")
        regular_file(payload, "guest payload", MAX_FRAME)
        regular_file(kernel, "guest kernel", 256 * 1024**2)
        regular_file(initrd, "guest initramfs", 512 * 1024**2)
    except (OSError, ValueError) as exc:
        reasons.append(str(exc))
    if args.cpus < 1 or args.cpus > 8:
        reasons.append("cpus outside 1..8")
    try:
        memory_mib = int(args.memory.removesuffix("M"))
        if args.memory != f"{memory_mib}M" or not MIN_MEMORY_MIB <= memory_mib <= MAX_MEMORY_MIB:
            raise ValueError
    except ValueError:
        reasons.append("memory must be an integer MiB in 128M..8192M")
    try:
        version = subprocess.run([qemu, "--version"], capture_output=True, text=True, check=False)
    except OSError as exc:
        reasons.append(f"QEMU unavailable: {exc}")
        version = None
    version_text = version.stdout.strip() if version is not None else ""
    if version is not None and (version.returncode != 0 or not version_text):
        reasons.append("QEMU version probe failed or returned empty output")
    try:
        image_info = subprocess.run([qemu_img, "info", "--output=json", str(image)], capture_output=True, text=True, check=False)
        data_info = subprocess.run([qemu_img, "info", "--output=json", str(data)], capture_output=True, text=True, check=False)
        if image_info.returncode or data_info.returncode:
            reasons.append("qemu-img format probe failed")
        else:
            for label, result in (("image", image_info), ("data disk", data_info)):
                info = json.loads(result.stdout)
                if info.get("format") != "raw":
                    reasons.append(f"{label} format must be raw")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reasons.append(f"qemu-img unavailable or invalid output: {exc}")
    if reasons:
        print(json.dumps({"result": "blocked", "reasons": reasons, "qemu": qemu}, indent=2), file=sys.stderr)
        return 3
    token = args.instance_token or uuid.uuid4().hex
    if len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("invalid instance token")
    timeout = float(os.environ.get("HB02_START_TIMEOUT", "30"))
    if not 0 < timeout <= 300:
        raise ValueError("startup timeout must be in (0, 300] seconds")
    cmd = qemu_command(qemu, image, data, payload, kernel, initrd, args.memory, args.cpus)
    cmd.extend(["-fw_cfg", f"name=opt/havenbox.nonce,string={token}"])
    print(json.dumps({"result": "launching", "qemu_version": version_text.splitlines()[0] if version_text else None, "command": cmd,
                      "device_policy": {"network": "none", "root": "readonly", "shared_dirs": "none", "qmp": "none", "machine": "virt", "accel": "hvf", "guest_payload": str(payload)}}, indent=2), flush=True)
    # Only serial stdout participates in readiness; stderr remains diagnostic output.
    diagnostics = tempfile.TemporaryFile()  # noqa: SIM115 -- closed with child resources below
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=diagnostics)
    except OSError:
        diagnostics.close()
        raise
    try:
        await_guest_ready(proc, token, timeout)
        print(f"HB02_READY {token}", flush=True)
        assert proc.stdout is not None
        while chunk := proc.stdout.read(4096):
            print(f"guest: {chunk.decode(errors='replace')}", flush=True)
        return proc.wait()
    except (OSError, ValueError) as exc:
        print(f"blocked: {exc}", file=sys.stderr, flush=True)
        return 3
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        diagnostics.seek(0)
        for line in diagnostics:
            print(f"qemu stderr: {line.decode(errors='replace').rstrip()}", flush=True)
        diagnostics.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test")
    launch_parser = sub.add_parser("launch")
    for name in ("image", "data", "payload", "kernel", "initrd"):
        launch_parser.add_argument(f"--{name}", required=True)
    launch_parser.add_argument("--qemu")
    launch_parser.add_argument("--qemu-img")
    launch_parser.add_argument("--memory", default="1024M")
    launch_parser.add_argument("--cpus", type=int, default=2)
    launch_parser.add_argument("--instance-token")
    args = parser.parse_args()
    return self_test() if args.command == "self-test" else launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
