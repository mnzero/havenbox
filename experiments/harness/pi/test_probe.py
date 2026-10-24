"""HB-05 regressions: run with python3 -m unittest discover -s experiments/harness/pi."""
import json
import pathlib
import queue
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import probe


class VersionGateTests(unittest.TestCase):
    def run_fake(self, version, extra=(), version_exit=0):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            executable = root / "pi"
            marker = root / "rpc-started"
            executable.write_text(
                f"#!{sys.executable}\nimport pathlib, sys\n"
                f"if sys.argv[1:] == ['--version']:\n print({version!r})\n sys.exit({version_exit})\n"
                f"pathlib.Path({str(marker)!r}).touch()\n"
            )
            executable.chmod(0o755)
            result = subprocess.run(
                [sys.executable, str(pathlib.Path(probe.__file__)), "--pi", str(executable), *extra],
                capture_output=True, text=True, timeout=10,
            )
            self.assertFalse(marker.exists(), "unapproved version must never start RPC")
            return result

    def test_mismatch_stops_before_rpc(self):
        result = self.run_fake("9.9.9")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])["status"], "blocked")

    def test_expected_version_cannot_override_pin(self):
        result = self.run_fake("9.9.9", ["--expected-version", "9.9.9"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_failed_version_command_stops_even_with_matching_output(self):
        self.assertEqual(self.run_fake(probe.PINNED_VERSION, version_exit=1).returncode, 2)


class CancellationTests(unittest.TestCase):
    def run_case(self, terminal, alive_after=False, terminal_first=False):
        # Use the real response correlator; only transport and PID observation are simulated.
        rpc = probe.Rpc.__new__(probe.Rpc)
        rpc.events = queue.Queue()
        rpc.pending = []
        ack = {"id": "cancel-1", "type": "response", "command": "abort_bash", "success": True}
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            def send(value):
                if value["type"] == "bash":
                    (root / "sleep.pid").write_text("12345")
                else:
                    for event in ([terminal, ack] if terminal_first else [ack, terminal]):
                        if event is not None:
                            rpc.events.put(event)
            rpc.send = send
            observations = iter([True])
            with patch.object(probe, "process_alive", side_effect=lambda pid: next(observations, alive_after)):
                return probe.probe_cancellation(rpc, root, timeout=0.02)

    def terminal(self, cancelled=True, request_id="abort-bash-1"):
        return {"id": request_id, "type": "response", "command": "bash", "success": True,
                "data": {"cancelled": cancelled}}

    def test_ack_without_terminal_fails(self):
        self.assertEqual(self.run_case(None)["status"], "fail")

    def test_unrelated_cancelled_terminal_fails(self):
        self.assertEqual(self.run_case(self.terminal(request_id="unrelated"))["status"], "fail")

    def test_normal_completion_is_not_cancellation(self):
        self.assertEqual(self.run_case(self.terminal(cancelled=False))["status"], "fail")

    def test_cancelled_response_with_live_process_fails(self):
        self.assertEqual(self.run_case(self.terminal(), alive_after=True)["status"], "fail")

    def test_cancelled_response_and_stopped_process_pass_in_both_orders(self):
        for terminal_first in (True, False):
            with self.subTest(terminal_first=terminal_first):
                self.assertEqual(self.run_case(self.terminal(), terminal_first=terminal_first)["status"], "pass")


if __name__ == "__main__":
    unittest.main()
