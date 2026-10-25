"""Offline transport regressions; opt-in real pinned Kimi integration."""
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import acp_client as acp
from protocol_probe import fixture_outcome, run_probe


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path('/tmp').resolve())  # noqa: S108
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.home.chmod(0o700)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def helper(self, code, timeout=5):
        binary = self.home / 'helper'
        binary.write_text('#!' + sys.executable + '\n' + code)
        binary.chmod(0o700)
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        with patch.object(acp, 'EXECUTABLE', binary), patch.object(acp, 'EXECUTABLE_SHA256', digest):
            return acp.ACPClient(self.home, timeout=timeout)

    def test_out_of_order_response_ids(self):
        with self.helper('import sys,json\na=json.loads(input()); b=json.loads(input())\n'
                         'for x in [b,a]: print(json.dumps({"jsonrpc":"2.0","id":x["id"],"result":x["method"]}),flush=True)\n'
                         'import time;time.sleep(10)\n') as client:
            first = client.request('first', {})
            second = client.request('second', {})
            self.assertEqual(client.wait(first), 'first')
            self.assertEqual(client.wait(second), 'second')
        self.assertIsNotNone(client.process.returncode)

    def test_malformed_oversize_eof_and_timeout(self):
        cases = [('print("not-json",flush=True)', 'invalid JSON'),
                 ('import os;os.write(1,b"x"*70000)', 'frame budget'),
                 ('pass', 'stdout closed'),
                 ('import time;time.sleep(10)', 'deadline')]
        for code, error in cases:
            with self.subTest(error=error):
                with self.helper(code) as client, self.assertRaisesRegex(acp.ProtocolError, error):
                    client.wait(client.request('initialize', {}))
                self.assertIsNotNone(client.process.returncode)

    def test_write_deadline_and_budget(self):
        with self.helper('import time;time.sleep(10)', timeout=0.3) as client:
            with self.assertRaisesRegex(acp.ProtocolError, 'write budget'):
                client.send({'large': 'x' * acp.MAX_FRAME})
            with self.assertRaisesRegex(acp.ProtocolError, 'write deadline'):
                for _ in range(32):
                    client.send({'large': 'x' * 60000})

    def test_environment_pin_and_override(self):
        env = acp.environment(self.home)
        self.assertNotIn('PYTHONPATH', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(env['KIMI_DISABLE_TELEMETRY'], '1')
        with patch.dict(os.environ, {'KIMI_CODE_HOME': ''}), patch.object(acp.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(acp.ProtocolError, 'overrides'):
                acp.ACPClient(self.home)
            launch.assert_not_called()
        with patch.object(acp, 'EXECUTABLE_SHA256', 'wrong'), patch.object(acp.subprocess, 'Popen') as launch:
            with self.assertRaises(acp.ProtocolError):
                acp.ACPClient(self.home)
            launch.assert_not_called()

    def test_dispatch_validation_budgets_and_permission_denial(self):
        with self.helper('import time;time.sleep(3)') as client, patch.object(acp.ACPClient, 'send') as send:
            client.dispatch({'jsonrpc': '2.0', 'id': 'approval', 'method': 'session/request_permission', 'params': {}})
            self.assertEqual(send.call_args.args[0]['result']['outcome']['outcome'], 'cancelled')
            client.dispatch({'jsonrpc': '2.0', 'id': 'fs', 'method': 'fs/read_text_file', 'params': {'path': '/etc/passwd'}})
            self.assertEqual(send.call_args.args[0]['error']['code'], -32601)
            for message in ([], {'jsonrpc': '2.0', 'id': 99, 'result': {}},
                            {'jsonrpc': '2.0', 'method': []}):
                with self.assertRaises(acp.ProtocolError):
                    client.dispatch(message)
            client.pending.add(1)
            client.dispatch({'jsonrpc': '2.0', 'id': 1, 'result': {}})
            with self.assertRaisesRegex(acp.ProtocolError, 'duplicate'):
                client.dispatch({'jsonrpc': '2.0', 'id': 1, 'result': {}})
            client.events = [{}] * acp.MAX_EVENTS
            with self.assertRaisesRegex(acp.ProtocolError, 'event budget'):
                client.dispatch({'jsonrpc': '2.0', 'method': 'session/update', 'params': {}})

    def test_stderr_total_budget_and_remote_error(self):
        with self.helper('import os,time\nos.write(2,b"x"*1100000)\ntime.sleep(3)', timeout=3) as client, self.assertRaisesRegex(acp.ProtocolError, 'read budget'):
            client.wait(client.request('initialize', {}))
        with self.helper('import json,time\nx=json.loads(input())\nprint(json.dumps({"jsonrpc":"2.0","id":x["id"],"error":{"code":-32602,"message":"secret"}}),flush=True)\ntime.sleep(3)') as client:
            with self.assertRaises(acp.RemoteError) as caught:
                client.call('bad', {})
            self.assertEqual(caught.exception.code, -32602)
            self.assertNotIn('secret', str(caught.exception))

    def test_prompt_completion_is_not_business_success(self):
        for reason, expected in [('end_turn', 'unknown'), ('cancelled', 'cancelled')]:
            with self.subTest(reason=reason), self.helper('import time;time.sleep(3)') as client:
                client.sessions.add('local')
                prompt = client.prompt('local', 'synthetic')
                client.dispatch({'jsonrpc': '2.0', 'id': prompt, 'result': {
                    'stopReason': reason, 'business_outcome': 'success'}})
                result = client.wait(prompt)
                self.assertEqual(result['protocol_status'], 'completed')
                self.assertEqual(result['business_outcome'], expected)
                self.assertIsNone(client.active_prompt)

    def test_fixture_failure_evidence_and_unknown(self):
        response = {'business_outcome': 'unknown'}
        for evidence in ([{'http_status': 400}], [{'http_status': 200, 'stream_error_status': 400}]):
            self.assertEqual(fixture_outcome(response, evidence)['business_outcome'], 'failed')
        for evidence in ([], [{'http_status': 200}]):
            self.assertEqual(fixture_outcome(response, evidence)['business_outcome'], 'unknown')

    def test_negotiation_and_single_turn(self):
        with self.helper('import time;time.sleep(3)') as client:
            with self.assertRaisesRegex(acp.ProtocolError, 'initialize required'):
                client.new_session()
            with patch.object(client, 'call', return_value={'protocolVersion': 99}), self.assertRaisesRegex(acp.ProtocolError, 'unsupported'):
                client.initialize()
            client.sessions.add('local')
            client.prompt('local', 'synthetic')
            with self.assertRaisesRegex(acp.ProtocolError, 'already active'):
                client.prompt('local', 'second')
            with self.assertRaisesRegex(acp.ProtocolError, 'unknown session'):
                client.cancel('wrong')


@unittest.skipUnless(os.environ.get('HB04_REAL_ACP') == '1', 'explicit local fixture opt-in required')
class RealProtocolTests(unittest.TestCase):
    def test_pinned_kimi_local_protocol(self):
        result = run_probe()
        self.assertEqual(result['initialize']['agentInfo']['version'], '2.2.0')
        self.assertEqual(result['events']['stream']['text'], 'READY')
        self.assertEqual(result['events']['stop']['response']['stopReason'], 'cancelled')
        self.assertEqual(result['remote_errors'], {'hb04/unsupported': -32601, 'session/set_mode': -32602})
        self.assertTrue(result['process_reaped'])
        self.assertTrue(result['home_removed'])
        self.assertEqual(len(result['request_shapes']), 4)
        self.assertIs(result['managed_config']['background.keep_alive_on_exit'], False)
        self.assertEqual(result['events']['stream']['response']['business_outcome'], 'unknown')
        self.assertEqual(result['events']['stop']['response']['business_outcome'], 'cancelled')
        for mode in ('error', 'partial_error'):
            self.assertEqual(result['events'][mode]['response']['business_outcome'], 'unknown')
            self.assertEqual(result['events'][mode]['outcome']['business_outcome'], 'failed')
            self.assertTrue(result['events'][mode]['status'].startswith('blocked'))
        partial = result['events']['partial_error']['updates']
        self.assertTrue(any(u.get('content', {}).get('text') == 'READY' for u in partial))
        self.assertTrue(all(r['tools'] == [] for r in result['request_shapes']))
        # Pin observed failure behavior, not a false promise of error visibility.
        self.assertEqual(result['events']['error']['response']['stopReason'], 'end_turn')


if __name__ == '__main__':
    unittest.main()
