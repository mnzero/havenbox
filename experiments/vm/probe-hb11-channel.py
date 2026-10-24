#!/usr/bin/env python3
"""Diagnostic-only real QEMU hb.channel.v1 client with serial capture."""

import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parent
INSTANCE = "00112233445566778899aabbccddeeff"


def encode(payload: dict[str, str], request: bytes) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return b"HBX1" + struct.pack(">HBBQ", 1, 1, 0, 1) + request + struct.pack(">I", len(body)) + hashlib.sha256(body).digest() + body


def receive(channel: socket.socket) -> dict:
    header = receive_exact(channel, 68)
    if header[:4] != b"HBX1":
        raise RuntimeError("invalid reply magic")
    size = struct.unpack(">I", header[32:36])[0]
    payload = receive_exact(channel, size)
    if hashlib.sha256(payload).digest() != header[36:68]:
        raise RuntimeError("invalid reply hash")
    result = json.loads(payload)
    print(f"channel-reply={result}")
    return result


def receive_exact(channel: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = channel.recv(size - len(result))
        if not chunk:
            raise RuntimeError("guest channel closed")
        result.extend(chunk)
    return bytes(result)


def main() -> int:
    if len(sys.argv) not in (3, 4, 5):
        raise SystemExit(
            f"usage: {sys.argv[0]} IMAGE_DIR DATA.raw [harness|worker] "
            "[normal|lease-expiry|replay|device-drift|output-overflow|active-lease-expiry]"
        )
    image = Path(sys.argv[1]).resolve()
    data = Path(sys.argv[2]).resolve()
    role = sys.argv[3] if len(sys.argv) == 4 else "harness"
    if len(sys.argv) == 5:
        role = sys.argv[3]
    mode = sys.argv[4] if len(sys.argv) == 5 else "normal"
    if role not in ("harness", "worker"):
        raise SystemExit("role must be harness or worker")
    if mode not in (
        "normal", "lease-expiry", "replay", "device-drift",
        "output-overflow", "active-lease-expiry",
    ):
        raise SystemExit("unsupported probe mode")
    test_case = {
        "output-overflow": "output-overflow",
        "active-lease-expiry": "hold",
    }.get(mode, "normal")
    state = Path.home() / ("hb11-channel-probe-" + str(time.time_ns()))
    state.mkdir(mode=0o700)
    command = [
        str(ROOT / "tools/qemu-system-aarch64"), "-M", "virt", "-accel", "hvf", "-cpu", "host",
        "-smp", "2", "-m", "4096M", "-nodefaults", "-nographic", "-nic", "none", "-serial", "stdio",
        "-monitor", "none", "-kernel", str(image / "Image"), "-initrd", str(image / "initramfs-hb11"),
        "-append", f"console=ttyAMA0 root=/dev/vda ro init=/sbin/hb-init hb.instance={INSTANCE} hb.epoch=1 hb.synthetic_role={role} hb.synthetic_case={test_case}",
        "-drive", f"if=virtio,format=raw,file={image / 'root.raw'},readonly=on",
        "-drive", f"if=virtio,format=raw,file={data},snapshot=off",
        "-device", "virtio-serial-pci,id=hbserial",
    ]
    if mode == "device-drift":
        command += ["-drive", f"if=virtio,format=raw,file={image / 'root.raw'},readonly=on"]
    for name in ("control", "file", "inference"):
        channel_path = state / f"{name}.sock"
        command += ["-chardev", f"socket,id={name},path={channel_path},server=on,wait=off",
                    "-device", f"virtserialport,bus=hbserial.0,chardev={name},name=hb.{name}"]
    environment = os.environ.copy()
    environment["HB02_QEMU_SYSTEM_FRAMEWORK"] = str(ROOT / "tools/qemu-aarch64-softmmu")
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment
    )
    assert process.stdout is not None
    def copy_serial() -> None:
        for line in process.stdout:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    serial = threading.Thread(target=copy_serial, daemon=True)
    serial.start()
    channels: list[socket.socket] = []
    try:
        deadline = time.monotonic() + 5
        for name in ("control", "file", "inference"):
            path = state / f"{name}.sock"
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.001)
            channel = socket.socket(socket.AF_UNIX)
            channel.connect(str(path))
            channel.settimeout(10)
            channels.append(channel)
        control = channels[0]
        control.sendall(encode({"type": "Hello", "instance": INSTANCE}, bytes(range(1, 17))))
        if mode == "device-drift":
            try:
                receive(control)
            except (BrokenPipeError, ConnectionResetError, RuntimeError, TimeoutError):
                print("device-drift-rejected=true")
                return 0
            raise RuntimeError("guest accepted unexpected block device")
        receive(control)
        control.settimeout(2)
        if mode == "output-overflow":
            try:
                receive(control)
            except (BrokenPipeError, ConnectionResetError, RuntimeError, TimeoutError):
                print("output-overflow-killed=true")
                return 0
            raise RuntimeError("output overflow remained live")
        if mode == "active-lease-expiry":
            time.sleep(6)
            try:
                receive(control)
            except (BrokenPipeError, ConnectionResetError, RuntimeError, TimeoutError):
                print("active-lease-killed=true")
                return 0
            raise RuntimeError("active task survived guest lease expiry")
        if mode in ("lease-expiry", "replay"):
            event = receive(control)
            if event.get("event") != "SyntheticTaskCompleted":
                raise RuntimeError("synthetic task did not complete before terminal probe")
        if mode == "lease-expiry":
            time.sleep(6)
            try:
                control.sendall(encode({"type": "Heartbeat", "sequence": "1"}, bytes([17]) * 16))
                receive(control)
            except (BrokenPipeError, ConnectionResetError, RuntimeError, TimeoutError):
                print("lease-terminal=true")
                return 0
            raise RuntimeError("expired guest lease accepted traffic")
        if mode == "replay":
            repeated = encode({"type": "Heartbeat", "sequence": "1"}, bytes([17]) * 16)
            control.sendall(repeated)
            control.sendall(repeated)
            try:
                receive(control)
            except (BrokenPipeError, ConnectionResetError, RuntimeError, TimeoutError):
                print("replay-terminal=true")
                return 0
            raise RuntimeError("replayed guest request remained live")
        for sequence in range(1, 5):
            control.sendall(encode({"type": "Heartbeat", "sequence": str(sequence)}, bytes([sequence + 16]) * 16))
            try:
                reply = receive(control)
                if reply.get("event") == "SyntheticTaskCompleted":
                    break
            except TimeoutError:
                continue
        control.sendall(encode({"type": "Stop"}, bytes([32]) * 16))
        receive(control)
        return 0
    finally:
        for channel in channels:
            channel.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        serial.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
