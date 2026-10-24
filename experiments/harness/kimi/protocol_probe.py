#!/usr/bin/env python3
"""Explicit local-only ACP experiment; synthetic HTTP fixture is NOT a real model."""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from acp_client import EXECUTABLE_SHA256, ACPClient, ProtocolError, RemoteError


class Fixture(HTTPServer):
    def __init__(self):
        super().__init__(('127.0.0.1', 0), Handler)
        self.mode = 'text'
        self.requests = []
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release.set()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # no raw prompts, headers or credentials in diagnostic output

    def do_POST(self):
        self.connection.settimeout(5)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 262144 or len(self.server.requests) >= 8:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('object required')
        except (ValueError, OSError):
            self.send_error(400)
            return
        # Keep structural evidence only. Full hidden system prompt is not persisted.
        record = {'path': self.path, 'model': body.get('model'),
            'stream': body.get('stream'), 'roles': [m.get('role') for m in body.get('messages', [])],
            'keys': sorted(body), 'tools': [t.get('function', {}).get('name') for t in body.get('tools', [])],
            'fixture_mode': self.server.mode, 'http_status': 400 if self.server.mode == 'error' else 200,
            'stream_error_status': 400 if self.server.mode == 'partial_error' else None}
        self.server.requests.append(record)
        self.server.arrived.set()
        mode = self.server.mode
        if mode == 'error':
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"HB04 synthetic error","type":"invalid_request_error"}}')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if mode == 'hold':
            self.server.release.wait(10)
            return
        chunks = [({'role': 'assistant', 'content': 'READY'}, None)]
        if mode != 'partial_error':
            chunks.append(({}, 'stop'))
        for delta, finish in chunks:
            payload = {'id': 'hb04', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': 'hb04-fixture', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
            self.wfile.write(('data: ' + json.dumps(payload) + '\n\n').encode())
            self.wfile.flush()
        if mode == 'partial_error':
            # HTTP headers are already 200: a later 400 can only be an SSE
            # error payload, never a second HTTP response status.
            self.wfile.write(b'data: {"error":{"message":"HB04 synthetic stream failure","type":"invalid_request_error","code":400}}\n\n')
            self.wfile.flush()
            return
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()


def fixture_outcome(response, requests):
    """Local fixture evidence only, scoped by the caller's serial turn window.

    This is not a provider gateway or a general-purpose success adjudicator.
    No evidence (or text alone) can promote the client's unknown to success.
    """
    failed = any(r['http_status'] >= 400 or r.get('stream_error_status') for r in requests)
    return {'business_outcome': 'failed' if failed else response['business_outcome'],
            'evidence_scope': 'local synthetic fixture only',
            'request_count': len(requests)}


def run_probe():
    result = {'probe': 'HB-04-local-acp', 'scope': 'host synthetic loopback; NOT provider or VM acceptance',
              'binary_sha256': EXECUTABLE_SHA256, 'events': {}}
    with Fixture() as fixture, tempfile.TemporaryDirectory(
            prefix='hb04-acp-', dir=Path('/tmp').resolve()) as directory:  # noqa: S108 -- exclusive mkdtemp, not a predictable child
        home = Path(directory)
        home.chmod(0o700)
        config = Path(__file__).with_name('managed.toml').read_text()
        config = config.replace('http://127.0.0.1:1/v1', f'http://127.0.0.1:{fixture.server_port}/v1')
        (home / 'config.toml').write_text(config)
        (home / 'config.toml').chmod(0o600)
        effective = tomllib.loads((home / 'config.toml').read_text())
        if effective.get('background', {}).get('keep_alive_on_exit') is not False or 'task' in effective:
            raise ProtocolError('managed background config mismatch')
        result['managed_config'] = {'background.keep_alive_on_exit': False,
                                    'verification': 'written TOML + pinned upstream schema; not runtime introspection'}
        with ACPClient(home) as client:
            init = client.initialize()
            result['initialize'] = {k: init[k] for k in ('protocolVersion', 'agentInfo', 'agentCapabilities')}
            session = client.new_session()['sessionId']
            result['events']['session'] = 'pass'
            for method, params in [('hb04/unsupported', {}), ('session/set_mode', {'sessionId': session, 'modeId': 'hb04-invalid'})]:
                try:
                    client.call(method, params)
                    raise ProtocolError('invalid request unexpectedly succeeded')
                except RemoteError as error:
                    result.setdefault('remote_errors', {})[method] = error.code
            prompt = client.prompt(session, 'HB-04 synthetic protocol probe. Reply READY; no files or tools.')
            response = client.wait(prompt)
            chunks = [e.get('params', {}).get('update', {}).get('content', {}).get('text', '')
                      for e in client.events if e.get('params', {}).get('update', {}).get('sessionUpdate') == 'agent_message_chunk']
            if response.get('stopReason') != 'end_turn' or ''.join(chunks) != 'READY':
                raise ProtocolError('stream evidence mismatch')
            result['events']['stream'] = {'status': 'pass', 'text': ''.join(chunks), 'response': response}
            fixture.mode = 'hold'
            fixture.arrived.clear()
            prompt = client.prompt(session, 'HB-04 synthetic cancellation probe.')
            deadline = time.monotonic() + 10
            while not fixture.arrived.is_set():
                client.pump(deadline)
            client.cancel(session)
            cancelled = client.wait(prompt)
            if cancelled.get('stopReason') != 'cancelled':
                raise ProtocolError('cancel evidence mismatch')
            fixture.release.set()
            result['events']['stop'] = {'status': 'pass', 'response': cancelled}
            for mode in ('error', 'partial_error'):
                fixture.mode = mode
                error_start = len(client.events)
                request_start = len(fixture.requests)
                try:
                    error_response = client.wait(client.prompt(session, 'HB-04 synthetic provider error probe.'))
                    result['events'][mode] = {
                        'status': 'blocked: upstream failure not surfaced as ACP error',
                        'response': error_response,
                        'outcome': fixture_outcome(error_response, fixture.requests[request_start:]),
                        'updates': [e.get('params', {}).get('update') for e in client.events[error_start:]]}
                except RemoteError as error:
                    result['events'][mode] = {'status': 'pass', 'code': error.code,
                                              'outcome': {'business_outcome': 'failed'}}
            result['request_shapes'] = fixture.requests
            result['events']['tool_rejection'] = 'unknown: no actual tool call in this run'
            # Local-only diagnostic: never trust disk state as authorization or replay tools.
            result['history_files'] = [str(p.relative_to(home)) for p in home.rglob('*.jsonl')][:20]
            result['transport'] = {'read_bytes': client.total, 'written_bytes': client.sent,
                                   'stderr_bytes_discarded': client.stderr_bytes, 'events': len(client.events)}
        result['process_reaped'] = client.process.returncode is not None
    result['home_removed'] = not home.exists()
    result['overall'] = 'partial: local protocol passed; provider/VM/tool boundary unknown'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-fixture', action='store_true', help='explicitly authorize synthetic local HTTP protocol test')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not args.local_fixture:
        parser.error('--local-fixture is required; no default subprocess or network execution')
    try:
        result = run_probe()
        code = 0
    except (ProtocolError, OSError, ValueError) as error:
        result = {'overall': 'blocked', 'error_type': type(error).__name__}
        if isinstance(error, ProtocolError):
            result['reason'] = str(error)
        code = 1
    payload = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(payload)
    print(payload, end='')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
