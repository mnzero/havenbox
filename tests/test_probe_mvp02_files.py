import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "experiments/vm/probe-mvp02-files.py"
SPEC = importlib.util.spec_from_file_location("probe_mvp02_files", SCRIPT)
assert SPEC and SPEC.loader
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


def decode(frame: bytes) -> tuple[dict[str, str], bytes]:
    size = int.from_bytes(frame[32:36], "big")
    return json.loads(frame[68:68 + size]), frame[16:32]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class RecordingSocket:
    def __init__(self, clock: Clock, advance: float = 0.0) -> None:
        self.clock = clock
        self.advance = advance
        self.frames: list[bytes] = []

    def sendall(self, frame: bytes) -> None:
        self.frames.append(frame)
        self.clock.now += self.advance


class Process:
    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        return self.returncode  # type: ignore[return-value]


class SerialThread:
    def __init__(self) -> None:
        self.join_calls = 0

    def join(self, timeout: float) -> None:
        self.join_calls += 1

    def is_alive(self) -> bool:
        return False


class ConnectSocket:
    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.closed = False
        self.path: str | None = None

    def connect(self, path: str) -> None:
        self.path = path
        if self.error is not None:
            raise self.error

    def close(self) -> None:
        self.closed = True


def guest(clock: Clock, file_advance: float) -> tuple[object, RecordingSocket, RecordingSocket]:
    control = RecordingSocket(clock)
    file_channel = RecordingSocket(clock, file_advance)
    instance = PROBE.Guest.__new__(PROBE.Guest)
    instance.channels = [control, file_channel]
    instance.request_sequence = 1
    instance.heartbeat_sequence = 0
    instance.next_heartbeat_at = PROBE.HEARTBEAT_INTERVAL_SECONDS
    return instance, control, file_channel


class GuestCloseTests(unittest.TestCase):
    def make_guest(self, parent: Path, returncode: int | None) -> object:
        state = Path(tempfile.mkdtemp(prefix="mvp02-qemu-", dir=parent)).resolve(strict=True)
        os.chmod(state, 0o700)
        state_stat = state.lstat()
        instance = PROBE.Guest.__new__(PROBE.Guest)
        instance.state = state
        instance.state_identity = (state_stat.st_dev, state_stat.st_ino)
        instance.process = Process(returncode)
        instance.serial = SerialThread()
        instance.channels = []
        return instance

    @staticmethod
    def create_sockets(state: Path) -> list[socket.socket]:
        sockets = []
        for name in PROBE.STATE_SOCKET_NAMES:
            channel = socket.socket(socket.AF_UNIX)
            channel.bind(str(state / name))
            sockets.append(channel)
        return sockets

    def test_process_exit_removes_all_sockets_and_empty_state_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = self.make_guest(Path(directory), returncode=-9)
            sockets = self.create_sockets(instance.state)
            try:
                instance.close(kill=True)
            finally:
                for channel in sockets:
                    channel.close()

            self.assertFalse(instance.state.exists())
            self.assertEqual(instance.process.kill_calls, 0)
            self.assertEqual(instance.serial.join_calls, 1)

    def test_running_process_is_stopped_before_state_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = self.make_guest(Path(directory), returncode=None)
            sockets = self.create_sockets(instance.state)
            try:
                instance.close()
            finally:
                for channel in sockets:
                    channel.close()

            self.assertFalse(instance.state.exists())
            self.assertEqual(instance.process.terminate_calls, 1)
            self.assertEqual(instance.process.wait_calls, 1)

    def test_non_socket_known_entry_is_refused_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for kind in ("regular file", "symbolic link"):
                with self.subTest(kind=kind):
                    instance = self.make_guest(Path(directory), returncode=0)
                    path = instance.state / "control.sock"
                    if kind == "regular file":
                        path.write_text("not a socket")
                    else:
                        path.symlink_to(instance.state / "missing-target")

                    with self.assertRaisesRegex(RuntimeError, "non-socket QEMU state entry"):
                        instance.close()

                    self.assertTrue(instance.state.exists())
                    self.assertTrue(path.is_symlink() if kind == "symbolic link" else path.is_file())

    def test_repeated_close_after_cleanup_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            instance = self.make_guest(Path(directory), returncode=0)
            sockets = self.create_sockets(instance.state)
            try:
                instance.close()
                instance.close()
            finally:
                for channel in sockets:
                    channel.close()

            self.assertFalse(instance.state.exists())
            self.assertEqual(instance.serial.join_calls, 2)


class GuestConnectTests(unittest.TestCase):
    def make_guest(self, returncode: int | None = None) -> object:
        instance = PROBE.Guest.__new__(PROBE.Guest)
        instance.state = Path("/tmp/connect-test")
        instance.process = Process(returncode)
        return instance

    def test_connection_refused_is_retried_then_succeeds_and_closes_failed_socket(self) -> None:
        instance = self.make_guest()
        refused = ConnectSocket(ConnectionRefusedError(PROBE.errno.ECONNREFUSED, "refused"))
        connected = ConnectSocket()
        sockets = iter([refused, connected])

        with mock.patch.object(PROBE.socket, "socket", side_effect=lambda *_: next(sockets)), \
             mock.patch.object(PROBE.time, "sleep"):
            result = instance._connect_channel("control", PROBE.time.monotonic() + 1)

        self.assertIs(result, connected)
        self.assertTrue(refused.closed)
        self.assertFalse(connected.closed)

    def test_continuous_refusal_times_out_with_channel_and_last_error(self) -> None:
        instance = self.make_guest()
        clock = Clock()
        created: list[ConnectSocket] = []

        def make_socket(*_args) -> ConnectSocket:
            channel = ConnectSocket(
                ConnectionRefusedError(PROBE.errno.ECONNREFUSED, "still refused")
            )
            created.append(channel)
            return channel

        def sleep(seconds: float) -> None:
            clock.now += seconds

        with mock.patch.object(PROBE.socket, "socket", side_effect=make_socket), \
             mock.patch.object(PROBE.time, "monotonic", clock.monotonic), \
             mock.patch.object(PROBE.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(
                RuntimeError, r"timed out connecting file channel; last error: .*still refused"
            ):
                instance._connect_channel("file", 0.02)

        self.assertGreaterEqual(len(created), 2)
        self.assertTrue(all(channel.closed for channel in created))

    def test_retry_stops_when_qemu_exits_and_reports_last_error(self) -> None:
        instance = self.make_guest()
        refused = ConnectSocket(ConnectionRefusedError(PROBE.errno.ECONNREFUSED, "refused"))

        def sleep(_seconds: float) -> None:
            instance.process.returncode = 1

        with mock.patch.object(PROBE.socket, "socket", return_value=refused), \
             mock.patch.object(PROBE.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(
                RuntimeError, r"QEMU exited while connecting inference channel; last error: .*refused"
            ):
                instance._connect_channel("inference", PROBE.time.monotonic() + 1)

        self.assertTrue(refused.closed)

    def test_non_retryable_socket_error_fails_immediately_and_closes_socket(self) -> None:
        instance = self.make_guest()
        denied = ConnectSocket(PermissionError(PROBE.errno.EACCES, "denied"))

        with mock.patch.object(PROBE.socket, "socket", return_value=denied), \
             mock.patch.object(PROBE.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                instance._connect_channel("control", PROBE.time.monotonic() + 1)

        self.assertTrue(denied.closed)
        sleep.assert_not_called()


class ProbeHeartbeatTests(unittest.TestCase):
    def test_long_transfer_renews_with_increasing_sequences_and_distinct_requests(self) -> None:
        clock = Clock()
        instance, control, file_channel = guest(clock, file_advance=1.1)
        contents = b"header\n" + b"value\n" * 12
        artifact_request = bytes.fromhex("80" + "00" * 15)
        replies = iter([
            ({
                "type": "InputAccepted", "fileId": "file_mvp02-input",
                "size": str(len(contents)), "sha256": PROBE.hashlib.sha256(contents).hexdigest(),
                "readOnly": "true",
            }, None),
            ({
                "type": "CommitArtifact", "fileId": "file_mvp02-result",
                "size": str(len(contents)), "sha256": PROBE.hashlib.sha256(contents).hexdigest(),
                "mediaType": "text/csv",
            }, artifact_request),
            ({
                "type": "FileChunk", "fileId": "file_mvp02-result", "offset": "0",
                "bytes": base64.b64encode(contents).decode(), "eof": "true",
            }, None),
        ])

        def receive(_channel: object, _expected_channel: int, expected_request=None):
            payload, response_request = next(replies)
            return payload, response_request or expected_request

        with mock.patch.object(PROBE.time, "monotonic", clock.monotonic), mock.patch.object(
            PROBE, "receive", receive
        ):
            digest, artifact = PROBE.transfer(instance, contents)

        self.assertEqual(artifact, contents)
        self.assertEqual(digest, PROBE.hashlib.sha256(contents).hexdigest())
        heartbeats = [decode(frame) for frame in control.frames]
        self.assertGreaterEqual(len(heartbeats), 3)
        self.assertEqual(
            [payload for payload, _ in heartbeats],
            [
                {"type": "Heartbeat", "sequence": str(sequence)}
                for sequence in range(1, len(heartbeats) + 1)
            ],
        )
        allocated_file_requests = [
            request_id for frame in file_channel.frames
            for payload, request_id in [decode(frame)]
            if payload["type"] != "ArtifactCommitted"
        ]
        allocated_requests = [request_id for _, request_id in heartbeats] + allocated_file_requests
        self.assertNotIn(bytes(16), allocated_requests)
        self.assertEqual(len(allocated_requests), len(set(allocated_requests)))

    def test_partial_transfer_returns_without_background_heartbeats(self) -> None:
        clock = Clock()
        instance, control, file_channel = guest(clock, file_advance=0.0)

        with mock.patch.object(PROBE.time, "monotonic", clock.monotonic):
            self.assertIsNone(PROBE.transfer(instance, b"short", partial=True))
            clock.now = 100.0

        self.assertEqual(control.frames, [])
        requests = [decode(frame)[1] for frame in file_channel.frames]
        self.assertEqual(len(requests), 2)
        self.assertNotIn(bytes(16), requests)
        self.assertEqual(len(requests), len(set(requests)))


class ProbeSerialMarkerTests(unittest.TestCase):
    @staticmethod
    def serial_guest(initial: bytes = b""):
        instance = PROBE.Guest.__new__(PROBE.Guest)
        instance.serial_output = bytearray(initial)
        instance.serial_condition = threading.Condition()
        return instance

    def test_marker_already_present_succeeds_immediately(self) -> None:
        instance = self.serial_guest(b"before exact-marker after")

        instance.wait_for_serial_marker(b"exact-marker", 0.1, "marker missing")

    def test_marker_appended_asynchronously_succeeds(self) -> None:
        instance = self.serial_guest(b"exact-mark")

        def append_marker() -> None:
            time.sleep(0.01)
            with instance.serial_condition:
                instance.serial_output.extend(b"er")
                instance.serial_condition.notify_all()

        writer = threading.Thread(target=append_marker)
        writer.start()
        try:
            instance.wait_for_serial_marker(b"exact-marker", 1, "marker missing")
        finally:
            writer.join()

    def test_marker_timeout_reports_exact_missing_marker(self) -> None:
        instance = self.serial_guest(b"exact-mark")

        with self.assertRaisesRegex(
            RuntimeError, r"marker missing; missing exact serial marker b'exact-marker'"
        ):
            instance.wait_for_serial_marker(b"exact-marker", 0.01, "marker missing")

    def test_lease_expiry_waits_for_exact_exit_status_before_close(self) -> None:
        events: list[object] = []
        first = mock.Mock()
        first.wait_for_cleanup.side_effect = lambda: events.append("cleanup")
        first.wait_for_serial_marker.side_effect = (
            lambda marker, timeout, message: events.append(("status", marker, timeout, message))
        )
        first.close.side_effect = lambda: events.append("first-close")
        second = mock.Mock()
        second.channels = [mock.Mock(), mock.Mock()]
        second.stop.side_effect = lambda: events.append("second-stop")
        second.close.side_effect = lambda: events.append("second-close")

        with tempfile.TemporaryDirectory() as directory:
            csv = Path(directory) / "fixture.csv"
            csv.write_bytes(b"header\nvalue\n")
            argv = ["probe-mvp02-files.py", "image", "data", str(csv), "lease-expiry"]
            output = io.StringIO()
            with mock.patch.object(PROBE, "boot", side_effect=[first, second]), mock.patch.object(
                PROBE, "transfer", return_value=None
            ), mock.patch.object(PROBE, "receive", side_effect=TimeoutError), mock.patch.object(
                sys, "argv", argv
            ), redirect_stdout(output):
                self.assertEqual(PROBE.main(), 0)

        status_event = next(event for event in events if isinstance(event, tuple))
        self.assertEqual(status_event[1], PROBE.SUPERVISOR_LEASE_EXIT_MARKER)
        self.assertEqual(status_event[2], 10)
        self.assertLess(events.index(status_event), events.index("first-close"))
        self.assertIn("lease-expiry-cleanup=true", output.getvalue())
        self.assertIn("supervisor-exit-status=78", output.getvalue())


class ProbeInputChunkTests(unittest.TestCase):
    @staticmethod
    def replies(contents: bytes):
        digest = PROBE.hashlib.sha256(contents).hexdigest()
        artifact_request = bytes.fromhex("80" + "00" * 15)
        artifact_chunks = [
            contents[offset:offset + PROBE.ARTIFACT_RAW_CHUNK]
            for offset in range(0, len(contents), PROBE.ARTIFACT_RAW_CHUNK)
        ]
        responses = [
            ({
                "type": "InputAccepted", "fileId": "file_mvp02-input",
                "size": str(len(contents)), "sha256": digest, "readOnly": "true",
            }, None),
            ({
                "type": "CommitArtifact", "fileId": "file_mvp02-result",
                "size": str(len(contents)), "sha256": digest, "mediaType": "text/csv",
            }, artifact_request),
        ]
        offset = 0
        for chunk in artifact_chunks:
            responses.append(({
                "type": "FileChunk", "fileId": "file_mvp02-result",
                "offset": str(offset), "bytes": base64.b64encode(chunk).decode(),
                "eof": "false",
            }, None))
            offset += len(chunk)
        responses.append(({
            "type": "FileChunk", "fileId": "file_mvp02-result",
            "offset": str(offset), "bytes": "", "eof": "true",
        }, None))
        return iter(responses)

    def test_chunk_boundary_mode_uses_host_default_and_stays_below_request_window(self) -> None:
        clock = Clock()
        instance, control, file_channel = guest(clock, file_advance=0.0)
        instance.stop = mock.Mock()
        instance.close = mock.Mock()
        fixture = b"header\nvalue\n"
        boundary_size = PROBE.ARTIFACT_RAW_CHUNK * 2
        contents = (fixture * ((boundary_size + len(fixture) - 1) // len(fixture)))[:boundary_size]
        replies = self.replies(contents)

        def receive(_channel: object, _expected_channel: int, expected_request=None):
            payload, response_request = next(replies)
            return payload, response_request or expected_request

        with tempfile.TemporaryDirectory() as directory:
            csv = Path(directory) / "fixture.csv"
            csv.write_bytes(fixture)
            argv = ["probe-mvp02-files.py", "image", "data", str(csv), "chunk-boundary"]
            with mock.patch.object(PROBE.time, "monotonic", clock.monotonic), mock.patch.object(
                PROBE, "receive", receive
            ), mock.patch.object(PROBE, "boot", return_value=instance), mock.patch.object(
                sys, "argv", argv
            ):
                self.assertEqual(PROBE.main(), 0)

        payloads = [decode(frame)[0] for frame in file_channel.frames]
        input_payloads = [
            payload for payload in payloads
            if payload["type"] in ("BeginInput", "FileChunk", "CommitInput")
            and payload.get("fileId") == "file_mvp02-input"
        ]
        input_chunks = [payload for payload in input_payloads if payload["type"] == "FileChunk"]
        self.assertEqual(len(input_payloads), 5)
        self.assertLess(len(input_payloads), 4096)
        self.assertEqual(
            [len(base64.b64decode(payload["bytes"])) for payload in input_chunks],
            [128 * 1024, 128 * 1024, 128 * 1024 - 4096],
        )
        self.assertEqual(control.frames, [])

    def test_default_small_input_uses_seventeen_byte_chunks(self) -> None:
        clock = Clock()
        instance, _, file_channel = guest(clock, file_advance=0.0)
        contents = b"a" * 35
        replies = self.replies(contents)

        def receive(_channel: object, _expected_channel: int, expected_request=None):
            payload, response_request = next(replies)
            return payload, response_request or expected_request

        with mock.patch.object(PROBE.time, "monotonic", clock.monotonic), mock.patch.object(
            PROBE, "receive", receive
        ):
            PROBE.transfer(instance, contents)

        input_chunks = [
            payload for frame in file_channel.frames
            for payload, _ in [decode(frame)]
            if payload["type"] == "FileChunk" and payload["fileId"] == "file_mvp02-input"
        ]
        self.assertEqual(
            [len(base64.b64decode(payload["bytes"])) for payload in input_chunks],
            [17, 17, 1],
        )

    def test_invalid_input_chunk_sizes_are_rejected_before_sending(self) -> None:
        for chunk_size in (0, -1, PROBE.MAXIMUM_INPUT_CHUNK_BYTES + 1, True):
            with self.subTest(chunk_size=chunk_size):
                clock = Clock()
                instance, control, file_channel = guest(clock, file_advance=0.0)
                with self.assertRaises(ValueError):
                    PROBE.transfer(instance, b"input", input_chunk_bytes=chunk_size)
                self.assertEqual(control.frames, [])
                self.assertEqual(file_channel.frames, [])


if __name__ == "__main__":
    unittest.main()
