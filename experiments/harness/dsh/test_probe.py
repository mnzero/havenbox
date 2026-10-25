"""Probe-only regressions; fake launchers are never dsh runtime evidence."""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import probe


class ProbeTests(unittest.TestCase):
    def run_fake(self, body, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "launcher"
            # macOS may delay the first launch of a new executable; leave startup
            # headroom and prove the fixture ran, rather than timing out at exec.
            launcher.write_text(f"#!{sys.executable}\n"
                                "from pathlib import Path\nPath('ready').touch()\n" + body)
            launcher.chmod(0o700)
            started = time.monotonic()
            result, code = probe.rpc_probe(launcher, os.environ.copy(), root,
                                           timeout=2, **kwargs)
            self.assertLess(time.monotonic() - started, 4)
            self.assertTrue((root / "ready").exists(), "fixture never started")
            self.assertEqual(code, -signal.SIGKILL)
            return result

    def test_no_output_times_out(self):
        result = self.run_fake("import time\ntime.sleep(30)\n")
        self.assertEqual(result["readStatus"], "timeout")
        self.assertIsNone(result["initializeResponse"])

    def test_partial_line_times_out(self):
        result = self.run_fake('import os,time\nos.write(1,b\'{"jsonrpc":\')\ntime.sleep(30)\n')
        self.assertEqual(result["readStatus"], "timeout")
        self.assertIsNone(result["initializeResponse"])

    def test_output_is_bounded(self):
        for fd in (1, 2):
            with self.subTest(fd=fd):
                result = self.run_fake(f"import os,time\nos.write({fd},b'x'*8192)\ntime.sleep(30)\n",
                                       max_output=1024)
                self.assertEqual(result["readStatus"], "output_limit")

    def test_complete_response_does_not_wait_for_stderr_eof(self):
        result = self.run_fake('import os,time\nos.write(1,b\'{"jsonrpc":"2.0","id":1,"result":{}}\\n\')\ntime.sleep(30)\n')
        self.assertEqual(result["readStatus"], "response")
        self.assertEqual(result["initializeResponse"]["id"], 1)

    def test_timeout_kills_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "launcher"
            # Separate cold exec/fork startup from the two-second RPC deadline.
            # The child cannot start its survival timer until the parent handshake.
            launcher.write_text(f"#!{sys.executable}\nimport os,time\n"
                                "if os.fork() == 0:\n"
                                " open('child-ready', 'w').close()\n"
                                " while not os.path.exists('start'): time.sleep(0.01)\n"
                                " time.sleep(3)\n"
                                " open('survived', 'w').close()\n"
                                " time.sleep(30)\n"
                                "time.sleep(30)\n")
            launcher.chmod(0o700)
            try:
                process = subprocess.Popen(
                    [str(launcher), '--profile', 'sdk-minimal'], cwd=root,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as error:
                reason = {'code': 'descendant_startup_unavailable', 'detail': str(error)}
                print('HB_TEST_BLOCKED ' + json.dumps({'status': 'blocked', 'reason': reason}), flush=True)
                self.skipTest(reason['code'])
            try:
                startup_deadline = time.monotonic() + 15
                while not (root / 'child-ready').exists():
                    if process.poll() is not None or time.monotonic() >= startup_deadline:
                        reason = {'code': 'descendant_startup_unavailable',
                                  'detail': 'child-ready handshake not observed within 15s',
                                  'launcher_exit_code': process.poll()}
                        print('HB_TEST_BLOCKED ' + json.dumps({'status': 'blocked', 'reason': reason}), flush=True)
                        self.skipTest(reason['code'])
                    time.sleep(0.01)
                (root / 'start').touch()
                started = time.monotonic()
                # Only reuse the real, ready process; RPC I/O and group kill remain real.
                with mock.patch.object(probe.subprocess, 'Popen', return_value=process):
                    result, code = probe.rpc_probe(launcher, os.environ.copy(), root, timeout=2)
                self.assertLess(time.monotonic() - started, 4)
                self.assertEqual(result['readStatus'], 'timeout')
                self.assertEqual(code, -signal.SIGKILL)
                time.sleep(3.2)
                self.assertFalse((root / 'survived').exists())
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_descendant_launch_failure_reports_blocked(self):
        output = io.StringIO()
        with (mock.patch.object(probe.subprocess, 'Popen', side_effect=OSError('fixture exec denied')),
              contextlib.redirect_stdout(output), self.assertRaises(unittest.SkipTest)):
            self.test_timeout_kills_descendants()
        record = json.loads(output.getvalue().removeprefix('HB_TEST_BLOCKED '))
        self.assertEqual(record['status'], 'blocked')
        self.assertEqual(record['reason']['code'], 'descendant_startup_unavailable')

    def test_failed_validation_never_creates_home_or_env(self):
        original = probe.load_manifest()
        for failure in ("missing", "hash", "version", "commit", "lockfile"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                manifest = dict(original)
                if failure == "missing":
                    # Missing files must block even with approved hash fields.
                    manifest.update(launcherSha256="a" * 64, lockfileSha256="b" * 64)
                if failure in ("version", "commit", "lockfile"):
                    manifest[failure] = "wrong"
                if failure == "hash":
                    binary = root / manifest["launcher"]
                    binary.parent.mkdir(parents=True)
                    binary.write_text("unused")
                    binary.chmod(0o700)
                    (root / manifest["lockfile"]).write_text("{}")
                    manifest.update(launcherSha256="wrong", lockfileSha256="wrong")
                target = root / "result.json"
                with (mock.patch.object(probe, "ROOT", root),
                      mock.patch.object(probe, "load_manifest", return_value=manifest),
                      mock.patch.object(probe, "create_home") as home,
                      mock.patch.object(probe.os, "environ") as environ,
                      mock.patch.object(probe, "rpc_probe") as rpc,
                      mock.patch.object(sys, "argv", ["probe", "--json", str(target)]),
                      contextlib.redirect_stdout(io.StringIO())):
                    self.assertEqual(probe.main(), 3)
                    home.assert_not_called()
                    environ.copy.assert_not_called()
                    rpc.assert_not_called()
                record = json.loads(target.read_text())
                self.assertEqual(record["result"], "blocked")
                if failure in ("missing", "hash"):
                    self.assertFalse(record["homeCreated"])
                    self.assertIsNone(record["environment"])

    def test_sequential_requests_keep_stdin_open(self):
        requests = [{"jsonrpc": "2.0", "id": i, "method": "test"} for i in (1, 2)]
        result = self.run_fake(
            'import sys,json,time\n'
            'for _ in range(2):\n'
            ' r=json.loads(sys.stdin.readline())\n'
            ' print(json.dumps({"jsonrpc":"2.0","method":"event"}),flush=True)\n'
            ' print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":{}}),flush=True)\n'
            'time.sleep(30)\n', requests=requests)
        self.assertEqual(result["readStatus"], "response")
        self.assertEqual(len(result["messages"]), 4)

    def test_initialize_error_is_not_success(self):
        checks = probe.protocol_checks({"messages": [{"id": 1, "error": {"code": -32603}}],
                                        "readStatus": "response"}, Path("/unused"))
        self.assertFalse(checks["initialize"])
        self.assertFalse(all(checks.values()))

    def test_contract_version_matches(self):
        contract = json.loads((probe.ROOT / "contract.json").read_text())
        self.assertEqual(contract["adapterVersion"], probe.ADAPTER)


if __name__ == "__main__":
    unittest.main()
