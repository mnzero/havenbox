#!/usr/bin/env python3
"""Host-only security regressions; fake QEMU is never VM acceptance evidence."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("start.sh", "stop.sh", "clean.sh", "run.sh", "probe.py", "lifecycle.py"):
            shutil.copy2(ROOT / name, self.root / name)
        self.qemu = self.root / "fake-qemu"
        self.qemu.write_text('''#!/usr/bin/env python3
import os, sys, time
from pathlib import Path
if "--version" in sys.argv:
    print("fake QEMU: regression fixture only")
    sys.exit(0)
Path(__file__).with_suffix(".pid").write_text(str(os.getpid()))
token = next(a.split("string=", 1)[1] for a in sys.argv if a.startswith("name=opt/havenbox.nonce,"))
mode = os.environ["FAKE_MODE"]
if mode == "exit":
    sys.exit(0)
if mode == "wrong":
    print("HB02_GUEST_READY wrong", flush=True)
elif mode == "spoof":
    print("HB02_READY " + token, flush=True)
elif mode == "stderr-spoof":
    print("HB02_READY " + token, file=sys.stderr, flush=True)
elif mode == "stderr":
    print("HB02_GUEST_READY " + token, file=sys.stderr, flush=True)
elif mode in ("ready", "ready-exit"):
    print("HB02_GUEST_READY " + token, flush=True)
    if mode == "ready-exit":
        sys.exit(0)
time.sleep(60)
''')
        self.qemu.chmod(0o755)
        img = self.root / "fake-qemu-img"
        img.write_text('#!/bin/sh\nprintf \'{"format":"raw"}\\n\'\n')
        img.chmod(0o755)
        self.args = ["--qemu", str(self.qemu), "--qemu-img", str(img)]
        for flag, name in (("image", "guest.raw"), ("data", "data.raw"), ("kernel", "Image"), ("initrd", "initrd"), ("payload", "payload")):
            path = self.root / name
            with path.open("wb") as f:
                f.truncate(1024 * 1024 if flag == "data" else 1)
            self.args.extend([f"--{flag}", str(path)])
        self.pidfile = self.root / "state/qemu.pid"

    def run_script(self, name, args=(), mode="timeout"):
        return subprocess.run([str(self.root / name), *args], capture_output=True, text=True,
                              env={**os.environ, "HB02_START_TIMEOUT": "4", "FAKE_MODE": mode}, timeout=10)

    def assert_qemu_stopped(self):
        pid = int(self.qemu.with_suffix(".pid").read_text())
        result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        self.assertTrue(result.returncode != 0 or result.stdout.strip().startswith("Z"), result.stdout)

    def test_fail_closed_and_cleanup(self):
        for mode in ("exit", "timeout", "wrong", "spoof", "stderr", "stderr-spoof", "ready-exit"):
            with self.subTest(mode=mode):
                result = self.run_script("start.sh", self.args, mode)
                self.assertEqual(result.returncode, 3, result)
                self.assertNotIn("started verified", result.stdout)
                self.assertFalse(self.pidfile.exists())
                self.assert_qemu_stopped()

    def test_simulated_serial_success_and_stop(self):
        result = self.run_script("start.sh", self.args, "ready")
        self.addCleanup(lambda: self.run_script("stop.sh"))
        self.assertEqual(result.returncode, 0, result)
        self.assertTrue(self.pidfile.exists())
        self.assertIn("guest handshake received", result.stdout)
        self.assertEqual(self.run_script("stop.sh").returncode, 0)
        self.assertFalse(self.pidfile.exists())
        self.assert_qemu_stopped()
        self.assertEqual(self.run_script("clean.sh").returncode, 0)
        self.assertEqual((self.root / "data.raw").stat().st_size, 1024 * 1024)
        self.assertTrue((self.root / "guest.raw").exists())

    def test_stale_record_never_signals_unrelated_process(self):
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.terminate)
        self.pidfile.parent.mkdir()
        self.pidfile.write_text(json.dumps({"pid": child.pid, "token": "a" * 32,
                                           "identity": "wrong", "started_at": "wrong"}))
        result = self.run_script("stop.sh")
        self.assertEqual(result.returncode, 3)
        self.assertIsNone(child.poll())
        self.assertFalse(self.pidfile.exists())

    def test_symlink_state_rejected(self):
        target = self.root / "outside"
        target.mkdir()
        (self.root / "state").symlink_to(target, target_is_directory=True)
        self.assertEqual(self.run_script("start.sh", self.args).returncode, 3)
        self.assertEqual(list(target.iterdir()), [])

    def test_executable_entrypoint(self):
        self.assertTrue(os.access(ROOT / "start.sh", os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
