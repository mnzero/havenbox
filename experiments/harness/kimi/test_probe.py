"""Offline security regressions; helper processes are test-owned Python only."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import probe


class ProbeSecurityTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        self.home.chmod(0o700)

    def test_network_disabled_never_starts_even_versions(self):
        with patch.object(probe.subprocess, "Popen") as launch:
            result = probe.probe(self.home)
        launch.assert_not_called()
        self.assertEqual(result["overall"], "blocked")
        self.assertTrue(all(v["status"] == "blocked" for v in result["versions"].values()))

    def test_overrides_rejected_including_injection_and_empty(self):
        sentinel = self.home / "injected"
        for name in ("KIMI_CLI", "KIMI_ACP"):
            for value in ("", f"sh -c 'touch {sentinel}'", "kimi; echo token=supersecret",
                          "env PYTHONPATH=/evil kimi", "kimi"):
                with self.subTest(name=name, value=value), patch.dict(
                        os.environ, {name: value, "KIMI_ALLOW_NETWORK": "1"}), patch.object(
                            probe.subprocess, "Popen") as launch:
                    result = probe.probe(self.home)
                    self.assertEqual(result["overall"], "blocked")
                    self.assertNotIn("supersecret", json.dumps(result))
                    launch.assert_not_called()
        self.assertFalse(sentinel.exists())

    def test_empty_and_unapproved_commands_blocked(self):
        with patch.object(probe.subprocess, "Popen") as launch:
            for command in ((), ("/bin/sh", "-c", "echo bad")):
                result = probe.run(command, home=self.home, network_allowed=True)
                self.assertEqual(result["status"], "blocked")
            launch.assert_not_called()

    def test_shared_and_symlink_home_rejected_without_mutation(self):
        shared = self.home / "shared"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        link = self.home / "link"
        link.symlink_to(shared, target_is_directory=True)
        for directory in (shared, link):
            with patch.dict(os.environ, {"KIMI_CODE_HOME": str(directory), "KIMI_ALLOW_NETWORK": "1"}), patch.object(
                    probe.subprocess, "Popen") as launch:
                self.assertEqual(probe.probe(self.home)["overall"], "blocked")
                launch.assert_not_called()
            self.assertFalse(probe.private_home(directory))
        self.assertEqual(shared.stat().st_mode & 0o777, 0o755)

    def test_main_owns_and_removes_home(self):
        homes = []
        original = probe.probe

        def inspect(home):
            homes.append(home)
            self.assertTrue(probe.private_home(home))
            return original(home)

        output = self.home / "result.json"
        with patch.object(sys, "argv", ["probe", "--output", str(output)]), patch.object(
                probe, "probe", side_effect=inspect), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(probe.main(), 0)
        self.assertEqual(json.loads(output.read_text())["home_mode"], "0o700")
        self.assertFalse(homes[0].exists())

    def helper(self, code, **kwargs):
        # Only tests replace profiles, never production environment/configuration.
        command = (sys.executable, "-c", code)
        with patch.object(probe, "COMMANDS", {"test": command}):
            return probe.run(command, home=self.home, network_allowed=True, **kwargs)

    def test_output_redaction_and_clean_environment(self):
        with patch.dict(os.environ, {"CUSTOM_API_KEY": "bare-private-value", "PYTHONPATH": "/evil"}):
            result = self.helper("import os,sys; print('token=abc secret=def Bearer xyz bare-private-value'); "
                                 "print(os.environ.get('PYTHONPATH', 'clean')); "
                                 "print('password=hunter2', file=sys.stderr)")
        text = result["stdout"] + result["stderr"]
        for secret in ("abc", "def", "xyz", "bare-private-value", "hunter2", "/evil"):
            self.assertNotIn(secret, text)
        self.assertIn("clean", result["stdout"])
        self.assertEqual(result["status"], "pass")

    def test_bounded_capture_and_timeout(self):
        result = self.helper("import os; os.write(1, b'x' * 1000000)")
        self.assertIn("capture limit", result["reason"])
        self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), probe.CAPTURE_LIMIT)
        result = self.helper("import time; print('token=hidden', flush=True); time.sleep(10)", timeout=0.1)
        self.assertEqual(result["reason"], "command timed out")
        self.assertNotIn("hidden", json.dumps(result))

    def test_exception_does_not_echo_secrets(self):
        with patch.object(probe.subprocess, "Popen", side_effect=OSError("token=hidden")):
            result = probe.run(probe.COMMANDS["cli"], home=self.home, network_allowed=True)
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("hidden", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
