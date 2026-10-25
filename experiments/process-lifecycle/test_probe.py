"""Regression tests use real processes; never launchd/VM acceptance evidence."""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('hb03_probe', Path(__file__).with_name('probe.py'))
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class LifecycleTests(unittest.TestCase):
    def test_real_scenarios(self):
        for name in ('ui-kill', 'helper-kill', 'lease-expiry'):
            with self.subTest(name=name):
                result = probe.scenario(name)
                self.assertTrue(result['cleaned'], result['tracked_after'])

    def test_stalled_helper_is_failure_not_teardown_success(self):
        # Run a deliberately broken copy, with actual helper/child processes.
        # Previously reap() silently killed the stalled helper and returned pass.
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / 'probe.py'
            source = Path(probe.__file__).read_text()
            source = source.replace('if time.time() > lease_until:', 'if False:')
            broken.write_text(source)
            spec = importlib.util.spec_from_file_location('broken_hb03', broken)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with self.assertRaises(subprocess.TimeoutExpired):
                module.scenario('lease-expiry')
            self.assertFalse(any(str(broken) in str(row['command']) for row in probe.ps_snapshot()))

    def test_watcher_does_not_kill_unrelated_probe(self):
        unrelated = subprocess.Popen([sys.executable, str(probe.SELF), '--child'], start_new_session=True)
        try:
            result = probe.scenario('helper-kill')
            self.assertTrue(result['cleaned'])
            self.assertIsNone(unrelated.poll())
        finally:
            probe.cleanup_group(unrelated.pid)
            unrelated.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
