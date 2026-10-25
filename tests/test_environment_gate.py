"""Regression tests for the host test gate (no external network requests)."""
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('test_gate', ROOT / 'scripts/test-environment.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class GateTests(unittest.TestCase):
    def test_every_resolved_address_must_be_loopback(self):
        # Negative classification data only: never used to bind a socket.
        for address in ('0.0.0.0', '192.168.31.145', '10.0.0.1', '::', '::ffff:127.0.0.1', '::ffff:192.168.31.145'):  # noqa: S104
            with self.subTest(address=address), patch.object(gate.socket, 'getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, '', (address, 0)),
            ]), self.assertRaises(ValueError):
                gate.resolved_loopback('fixture.invalid')
        self.assertEqual(gate.resolved_loopback('127.0.0.1'), ['127.0.0.1'])
        self.assertEqual(gate.resolved_loopback('::1'), ['::1'])

    def test_pi_incomplete_checks_are_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'pi.log'
            for status in ('blocked', 'unknown', 'fail'):
                log.write_text(json.dumps({'status': status, 'name': 'fixture'}))
                entry = {'status': 'pass', 'exit_code': 0}
                gate.classify_pi(entry, log)
                self.assertEqual(entry['status'], 'fail' if status == 'fail' else 'blocked')
                self.assertEqual(entry['reason']['code'], 'pi_incomplete_checks')
                self.assertEqual(entry['exit_code'], 0)

    def test_dsh_startup_blocked_is_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'dsh.log'
            log.write_text('test ... HB_TEST_BLOCKED ' + json.dumps({
                'status': 'blocked', 'reason': {'code': 'descendant_startup_unavailable'}}) + '\nOK (skipped=1)\n')
            entry = {'status': 'pass', 'exit_code': 0}
            gate.classify_regression(entry, log)
            self.assertEqual(entry['status'], 'blocked')
            self.assertEqual(entry['reason']['code'], 'regression_startup_blocked')
            self.assertEqual(entry['exit_code'], 0)
            entry = {'status': 'fail', 'exit_code': 1}
            gate.classify_regression(entry, log)
            self.assertEqual(entry['status'], 'fail')

    @unittest.skipUnless(sys.platform == 'darwin', 'real Seatbelt required')
    def test_false_exit_matches_report_after_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'report'
            script = (f'import runpy; m=runpy.run_path({str(ROOT / "scripts/test-environment.py")!r}); '
                      f'raise SystemExit(m["main"](["--report-dir", {str(report)!r}], sandbox_command=["/usr/bin/false"]))')
            process = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True)
            result = json.loads((report / 'results.json').read_text())
            self.assertEqual(process.returncode, 1)
            self.assertEqual(result['exit_code'], process.returncode)
            self.assertEqual(result['checks'][0]['exit_code'], 1)
            self.assertTrue(result['scratch_removed'])
            self.assertEqual(result['residual_scratch_processes'], [])

    @unittest.skipUnless(sys.platform == 'darwin', 'real Seatbelt required')
    def test_loopback_request_is_blocked_before_harnesses(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'report'
            process = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/test-environment.py'),
                                      '--loopback', '--report-dir', str(report)], capture_output=True, text=True)
            result = json.loads((report / 'results.json').read_text())
            self.assertEqual(process.returncode, 2)
            self.assertEqual(result['exit_code'], 2)
            self.assertEqual([item['name'] for item in result['checks']], ['sandbox', 'loopback-policy'])
            self.assertEqual(result['checks'][1]['reason']['code'], 'exact_loopback_filter_unavailable')
            self.assertEqual(result['network'], 'denied-by-sandbox')
            self.assertTrue(result['scratch_removed'])

    @unittest.skipUnless(sys.platform == 'darwin', 'real Seatbelt required')
    def test_real_services_denied_including_non_loopback(self):
        address = subprocess.run(['/usr/sbin/ipconfig', 'getifaddr', 'en0'], capture_output=True, text=True).stdout.strip()
        self.assertTrue(address, 'non-loopback interface needed for real regression')
        with self.assertRaises(ValueError):
            gate.resolved_loopback(address)
        for host in ('127.0.0.1', address):
            with self.subTest(host=host), socket.socket() as server:
                server.bind((host, 0))
                server.listen()
                port = server.getsockname()[1]
                # Positive control proves a real reachable temporary service.
                with socket.create_connection((host, port), timeout=2):
                    connection, _ = server.accept()
                    connection.close()
                script = f'import socket; socket.create_connection(({host!r}, {port}), timeout=2)'
                process = subprocess.run(['/usr/bin/sandbox-exec', '-p', gate.sandbox_profile(True),
                                          sys.executable, '-B', '-c', script], capture_output=True, text=True)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('Operation not permitted', process.stderr)
                print(f'real sandbox target {host}:{port}: blocked')


if __name__ == '__main__':
    unittest.main()
