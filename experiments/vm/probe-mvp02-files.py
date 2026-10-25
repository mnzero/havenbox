#!/usr/bin/env python3
"""Real QEMU/HVF MVP-02 file round-trip and failure probe."""

import argparse
import base64
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[2]
INSTANCE = "102132435465768798a9bacbdcedfe0f"
EPOCH = 23
FRAME_LIMITS = {1: 65_536, 2: 262_144, 3: 4_194_304}
ARTIFACT_RAW_CHUNK = 190 * 1024
DEFAULT_INPUT_CHUNK_BYTES = 17
# Mirror Swift HostFileBoundary's default and maximum within the file-channel frame limit.
HOST_DEFAULT_INPUT_CHUNK_BYTES = 128 * 1024
MAXIMUM_INPUT_CHUNK_BYTES = 190 * 1024
# Leave three seconds of margin under the guest's fixed five-second lease.
HEARTBEAT_INTERVAL_SECONDS = 2.0
SUPERVISOR_LEASE_EXIT_MARKER = b"MVP02_SUPERVISOR_EXIT status=78"
STATE_SOCKET_NAMES = ("control.sock", "file.sock", "inference.sock")
BOOT_TIMEOUT_SECONDS = 30
CONNECT_RETRY_INTERVAL_SECONDS = 0.01
RETRYABLE_CONNECT_ERRNOS = (errno.ENOENT, errno.ECONNREFUSED)


def encode(channel: int, payload: dict[str, str], request: bytes) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return (
        b"HBX1" + struct.pack(">HBBQ", 1, channel, 0, EPOCH) + request
        + struct.pack(">I", len(body)) + hashlib.sha256(body).digest() + body
    )


def receive(
    channel: socket.socket, expected_channel: int, expected_request: bytes | None = None
) -> tuple[dict[str, str], bytes]:
    header = receive_exact(channel, 68)
    if header[:4] != b"HBX1":
        raise RuntimeError("invalid response magic")
    version, actual_channel, flags, epoch = struct.unpack(">HBBQ", header[4:16])
    size = struct.unpack(">I", header[32:36])[0]
    actual_request = header[16:32]
    if version != 1 or actual_channel != expected_channel or flags != 0 or epoch != EPOCH:
        raise RuntimeError("invalid response header binding")
    if expected_request is not None and actual_request != expected_request:
        raise RuntimeError("response request does not match operation")
    if actual_request == bytes(16) or size > FRAME_LIMITS[expected_channel]:
        raise RuntimeError("invalid response request or payload length")
    body = receive_exact(channel, size)
    if hashlib.sha256(body).digest() != header[36:68]:
        raise RuntimeError("invalid response payload hash")
    decoded = json.loads(body)
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in decoded.items()
    ):
        raise RuntimeError("invalid response payload shape")
    return decoded, actual_request


def receive_exact(channel: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = channel.recv(size - len(result))
        if not chunk:
            raise RuntimeError("guest channel closed")
        result.extend(chunk)
    return bytes(result)


def request(number: int) -> bytes:
    return number.to_bytes(16, "big")


class Guest:
    def __init__(self, image: Path, data: Path):
        self.state = Path(
            tempfile.mkdtemp(prefix="mvp02-qemu-", dir=Path.home())
        ).resolve(strict=True)
        os.chmod(self.state, 0o700)
        state_stat = self.state.lstat()
        if not self.state.is_absolute() or not stat.S_ISDIR(state_stat.st_mode):
            raise RuntimeError("QEMU state path is not an absolute private directory")
        self.state_identity = (state_stat.st_dev, state_stat.st_ino)
        command = [
            str(ROOT / "experiments/vm/tools/qemu-system-aarch64"),
            "-M", "virt", "-accel", "hvf", "-cpu", "host", "-smp", "2", "-m", "4096M",
            "-nodefaults", "-nographic", "-nic", "none", "-serial", "stdio", "-monitor", "none",
            "-kernel", str(image / "Image"), "-initrd", str(image / "initramfs-hb11"),
            "-append", (
                "console=ttyAMA0 root=/dev/vda ro init=/sbin/hb-init "
                f"hb.instance={INSTANCE} hb.epoch={EPOCH} hb.mode=files"
            ),
            "-drive", f"if=virtio,format=raw,file={image / 'root.raw'},readonly=on",
            "-drive", f"if=virtio,format=raw,file={data},snapshot=off",
            "-device", "virtio-serial-pci,id=hbserial",
        ]
        for name in ("control", "file", "inference"):
            path = self.state / f"{name}.sock"
            command += [
                "-chardev", f"socket,id={name},path={path},server=on,wait=off",
                "-device", f"virtserialport,bus=hbserial.0,chardev={name},name=hb.{name}",
            ]
        environment = os.environ.copy()
        environment["HB02_QEMU_SYSTEM_FRAMEWORK"] = str(
            ROOT / "experiments/vm/tools/qemu-aarch64-softmmu"
        )
        self.process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment
        )
        assert self.process.stdout is not None
        self.serial_output = bytearray()
        self.serial_condition = threading.Condition()
        self.serial = threading.Thread(target=self._copy_serial, daemon=True)
        self.serial.start()
        self.channels: list[socket.socket] = []
        self.request_sequence = 1
        self.heartbeat_sequence = 0
        self.next_heartbeat_at: float | None = None

    def _copy_serial(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            with self.serial_condition:
                self.serial_output.extend(line)
                self.serial_condition.notify_all()

    def allocate_request(self) -> bytes:
        value = request(self.request_sequence)
        self.request_sequence += 1
        return value

    def heartbeat_if_due(self) -> None:
        if self.next_heartbeat_at is None:
            return
        now = time.monotonic()
        if now < self.next_heartbeat_at:
            return
        sequence = self.heartbeat_sequence + 1
        heartbeat_request = self.allocate_request()
        self.channels[0].sendall(encode(1, {
            "type": "Heartbeat", "sequence": str(sequence),
        }, heartbeat_request))
        self.heartbeat_sequence = sequence
        self.next_heartbeat_at = now + HEARTBEAT_INTERVAL_SECONDS

    def wait_for_serial_marker(
        self, marker: bytes, timeout_seconds: float, failure_message: str
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        with self.serial_condition:
            while marker not in self.serial_output:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"{failure_message}; missing exact serial marker {marker!r} "
                        f"after {timeout_seconds:g} seconds"
                    )
                self.serial_condition.wait(timeout=remaining)

    def wait_for_cleanup(self) -> None:
        marker = b"MVP02_FAIL_CLOSED cgroup_empty=true incoming_empty=true tmp_empty=true"
        self.wait_for_serial_marker(
            marker, 10, "supervisor did not prove fail-closed cleanup in this VM"
        )

    def connect(self) -> None:
        deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
        for name in ("control", "file", "inference"):
            channel = self._connect_channel(name, deadline)
            channel.settimeout(10)
            self.channels.append(channel)
        hello_request = self.allocate_request()
        self.channels[0].sendall(
            encode(1, {"type": "Hello", "instance": INSTANCE}, hello_request)
        )
        reply, _ = receive(self.channels[0], 1, hello_request)
        if reply != {"type": "HelloAck", "instance": INSTANCE}:
            raise RuntimeError(f"unexpected hello: {reply}")
        self.next_heartbeat_at = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS

    def _connect_channel(self, name: str, deadline: float) -> socket.socket:
        path = self.state / f"{name}.sock"
        last_error: OSError | None = None
        while True:
            if self.process.poll() is not None:
                detail = f"; last error: {last_error}" if last_error is not None else ""
                raise RuntimeError(f"QEMU exited while connecting {name} channel{detail}")
            if time.monotonic() >= deadline:
                detail = str(last_error) if last_error is not None else "socket not ready"
                raise RuntimeError(
                    f"timed out connecting {name} channel; last error: {detail}"
                )

            channel = socket.socket(socket.AF_UNIX)
            try:
                channel.connect(str(path))
                return channel
            except OSError as error:
                channel.close()
                if error.errno not in RETRYABLE_CONNECT_ERRNOS:
                    raise
                last_error = error
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"QEMU exited while connecting {name} channel; last error: {error}"
                    ) from error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        f"timed out connecting {name} channel; last error: {error}"
                    ) from error
                time.sleep(min(CONNECT_RETRY_INTERVAL_SECONDS, remaining))

    def stop(self) -> None:
        if not self.channels:
            return
        self.heartbeat_if_due()
        stop_request = self.allocate_request()
        self.channels[0].sendall(encode(1, {"type": "Stop"}, stop_request))
        reply, _ = receive(self.channels[0], 1, stop_request)
        if reply.get("type") != "Stopped" or reply.get("taskProcesses") != "0":
            raise RuntimeError(f"stop did not prove cleanup: {reply}")
        self.wait_for_cleanup()

    def close(self, kill: bool = False) -> None:
        for channel in self.channels:
            channel.close()
        self.channels.clear()
        if self.process.poll() is None:
            self.process.kill() if kill else self.process.terminate()
            self.process.wait(timeout=5)
        if self.process.poll() is None:
            raise RuntimeError("QEMU process is still running after close")
        self.serial.join(timeout=1)
        if self.serial.is_alive():
            raise RuntimeError("QEMU serial thread is still running after process exit")
        self._remove_state_sockets()

    def _remove_state_sockets(self) -> None:
        try:
            state_stat = self.state.lstat()
        except FileNotFoundError:
            return
        if (
            not self.state.is_absolute()
            or not stat.S_ISDIR(state_stat.st_mode)
            or (state_stat.st_dev, state_stat.st_ino) != self.state_identity
            or stat.S_IMODE(state_stat.st_mode) != 0o700
        ):
            raise RuntimeError("refusing to clean replaced or non-private QEMU state directory")

        entries = {entry.name: entry for entry in self.state.iterdir()}
        unexpected = sorted(set(entries) - set(STATE_SOCKET_NAMES))
        if unexpected:
            raise RuntimeError(
                f"refusing to clean QEMU state directory with unexpected entries: {unexpected}"
            )
        for name, path in entries.items():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise RuntimeError(f"refusing to remove non-socket QEMU state entry: {name}")

        for name in STATE_SOCKET_NAMES:
            path = entries.get(name)
            if path is not None:
                path.unlink()
        self.state.rmdir()


def transfer(
    guest: Guest,
    contents: bytes,
    partial: bool = False,
    artifact_action: str = "ack",
    input_chunk_bytes: int = DEFAULT_INPUT_CHUNK_BYTES,
) -> tuple[str, bytes] | None:
    if (
        type(input_chunk_bytes) is not int
        or input_chunk_bytes <= 0
        or input_chunk_bytes > MAXIMUM_INPUT_CHUNK_BYTES
    ):
        raise ValueError(
            f"input chunk size must be between 1 and {MAXIMUM_INPUT_CHUNK_BYTES} bytes"
        )
    channel = guest.channels[1]
    digest = hashlib.sha256(contents).hexdigest()
    file_id = "file_mvp02-input"
    guest.heartbeat_if_due()
    begin_request = guest.allocate_request()
    channel.sendall(encode(2, {
        "type": "BeginInput", "fileId": file_id, "size": str(len(contents)),
        "sha256": digest, "mediaType": "text/csv",
    }, begin_request))
    sent = 0
    chunks = (
        contents[i:i + input_chunk_bytes]
        for i in range(0, len(contents), input_chunk_bytes)
    )
    for chunk in chunks:
        guest.heartbeat_if_due()
        chunk_request = guest.allocate_request()
        channel.sendall(encode(2, {
            "type": "FileChunk", "fileId": file_id, "offset": str(sent),
            "bytes": base64.b64encode(chunk).decode(),
        }, chunk_request))
        sent += len(chunk)
        if partial:
            return None
    guest.heartbeat_if_due()
    commit_input_request = guest.allocate_request()
    channel.sendall(
        encode(2, {"type": "CommitInput", "fileId": file_id}, commit_input_request)
    )
    accepted, _ = receive(channel, 2, commit_input_request)
    announced, commit_artifact_request = receive(channel, 2)
    if (
        accepted.get("type") != "InputAccepted"
        or accepted.get("fileId") != file_id
        or accepted.get("size") != str(len(contents))
        or accepted.get("readOnly") != "true"
    ):
        raise RuntimeError(f"input was not accepted read-only: {accepted}")
    if (
        announced.get("type") != "CommitArtifact"
        or announced.get("size") != str(len(contents))
        or announced.get("mediaType") != "text/csv"
        or accepted.get("sha256") != digest
        or announced.get("sha256") != digest
    ):
        raise RuntimeError("guest content hash differs from imported version")
    artifact_id = announced["fileId"]
    artifact = bytearray()
    while True:
        guest.heartbeat_if_due()
        read_request = guest.allocate_request()
        channel.sendall(encode(2, {
            "type": "ReadArtifact", "fileId": artifact_id, "offset": str(len(artifact)),
        }, read_request))
        returned, _ = receive(channel, 2, read_request)
        if returned.get("type") != "FileChunk" or returned.get("fileId") != artifact_id:
            raise RuntimeError(f"unexpected artifact chunk: {returned}")
        if returned.get("offset") != str(len(artifact)):
            raise RuntimeError("artifact chunk offset is not contiguous")
        chunk = base64.b64decode(returned["bytes"], validate=True)
        if len(chunk) > ARTIFACT_RAW_CHUNK:
            raise RuntimeError("artifact raw chunk exceeds bounded size")
        artifact.extend(chunk)
        if returned.get("eof") == "true":
            break
        if returned.get("eof") != "false" or not chunk:
            raise RuntimeError("artifact chunk did not make bounded progress")
    if artifact != contents or hashlib.sha256(artifact).hexdigest() != digest:
        raise RuntimeError("returned artifact differs from fixed input")
    acknowledgement = encode(2, {
        "type": "ArtifactCommitted", "fileId": artifact_id,
    }, commit_artifact_request)
    guest.heartbeat_if_due()
    if artifact_action == "ack":
        channel.sendall(acknowledgement)
    elif artifact_action == "wrong-request":
        channel.sendall(encode(2, {
            "type": "ArtifactCommitted", "fileId": artifact_id,
        }, guest.allocate_request()))
    elif artifact_action == "replay":
        channel.sendall(acknowledgement + acknowledgement)
    elif artifact_action == "stop":
        stop_request = guest.allocate_request()
        guest.channels[0].sendall(encode(1, {"type": "Stop"}, stop_request))
    else:
        raise RuntimeError(f"unknown artifact action: {artifact_action}")
    return digest, bytes(artifact)


def boot(image: Path, data: Path) -> Guest:
    guest = Guest(image, data)
    try:
        guest.connect()
    except Exception:
        guest.close(kill=True)
        raise
    return guest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("data", type=Path)
    parser.add_argument("csv", type=Path)
    parser.add_argument(
        "mode",
        choices=(
            "normal", "disconnect", "lease-expiry", "protocol-error", "ack-mismatch",
            "ack-replay", "unacked-stop", "chunk-boundary", "kill", "restart",
        ),
    )
    args = parser.parse_args()
    contents = args.csv.read_bytes()
    if args.mode == "chunk-boundary":
        if not contents:
            raise RuntimeError("fixed CSV fixture is empty")
        boundary_size = ARTIFACT_RAW_CHUNK * 2
        contents = (contents * ((boundary_size + len(contents) - 1) // len(contents)))[:boundary_size]

    first = boot(args.image.resolve(), args.data.resolve())
    try:
        if args.mode in ("disconnect", "lease-expiry", "protocol-error", "kill"):
            transfer(first, contents, partial=True)
            if args.mode == "disconnect":
                for channel in first.channels:
                    channel.close()
                first.channels.clear()
                first.wait_for_cleanup()
                print("host-disconnect-channel-closed=true")
            elif args.mode == "lease-expiry":
                first.wait_for_cleanup()
                print("lease-expiry-cleanup=true")
                first.wait_for_serial_marker(
                    SUPERVISOR_LEASE_EXIT_MARKER,
                    10,
                    "supervisor did not report the lease-expiry exit status in this VM",
                )
                print("supervisor-exit-status=78")
            elif args.mode == "protocol-error":
                malformed = bytearray(encode(2, {"type": "CommitInput"}, first.allocate_request()))
                malformed[5] = 2
                first.channels[1].sendall(malformed)
                first.wait_for_cleanup()
                print("protocol-error-cleanup=true")
            else:
                pid = first.process.pid
                first.close(kill=True)
                if first.process.poll() is None:
                    raise RuntimeError("QEMU survived SIGKILL")
                print(f"qemu-killed=true pid={pid} residual=false")
        elif args.mode in ("ack-mismatch", "ack-replay", "unacked-stop"):
            action = {
                "ack-mismatch": "wrong-request",
                "ack-replay": "replay",
                "unacked-stop": "stop",
            }[args.mode]
            transfer(first, contents, artifact_action=action)
            first.wait_for_cleanup()
            print(f"{args.mode}-rejected=true cleanup=true")
        else:
            input_chunk_bytes = (
                HOST_DEFAULT_INPUT_CHUNK_BYTES
                if args.mode == "chunk-boundary"
                else DEFAULT_INPUT_CHUNK_BYTES
            )
            digest, artifact = transfer(  # type: ignore[misc]
                first, contents, input_chunk_bytes=input_chunk_bytes
            )
            print(f"roundtrip=true input-readonly=true bytes={len(artifact)} sha256={digest}")
            if args.mode == "chunk-boundary":
                print("artifact-multichunk=true exact-boundary=true eof=true")
            first.stop()
            print("stop-ack=true task-processes=0")
    finally:
        first.close()

    if args.mode in (
        "disconnect", "lease-expiry", "protocol-error", "ack-mismatch", "ack-replay",
        "unacked-stop", "kill", "restart",
    ):
        second = boot(args.image.resolve(), args.data.resolve())
        try:
            second.channels[1].settimeout(1)
            try:
                unsolicited = receive(second.channels[1], 2)
            except TimeoutError:
                unsolicited = None
            if unsolicited is not None:
                raise RuntimeError(f"restart replayed file operation: {unsolicited}")
            second.stop()
            print("restart-replay=false task-processes=0")
        finally:
            second.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
