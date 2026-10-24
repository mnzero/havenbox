"""Bounded ACP v1 stdio transport. No host fs/terminal capabilities are granted."""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path

from probe import OVERRIDES, private_home

EXECUTABLE = Path('/Users/mnzero/.kimi-code/bin/kimi')
EXECUTABLE_SHA256 = '894901e44ce5c7a2f610991698350ef20d1ffd4214b9dea902bbd16b12afddae'
MAX_FRAME = 65536
MAX_TOTAL = 1048576
MAX_EVENTS = 256


class ProtocolError(RuntimeError):
    """Fixed diagnostic only: never echo untrusted frames or exception text."""


class RemoteError(ProtocolError):
    def __init__(self, code):
        self.code = code
        super().__init__(f'ACP remote error {code if type(code) is int else "invalid"}')


def environment(home: Path) -> dict:
    return {'HOME': str(home), 'KIMI_CODE_HOME': str(home), 'PATH': '/usr/bin:/bin',
            'LANG': 'C', 'TMPDIR': str(home), 'XDG_CONFIG_HOME': str(home),
            'XDG_CACHE_HOME': str(home), 'KIMI_DISABLE_TELEMETRY': '1',
            'KIMI_CODE_NO_AUTO_UPDATE': '1', 'KIMI_DISABLE_CRON': '1',
            # Defense in depth only, not a network sandbox. Only the fixture bypasses it.
            'HTTP_PROXY': 'http://127.0.0.1:1', 'HTTPS_PROXY': 'http://127.0.0.1:1',
            'ALL_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '127.0.0.1'}


class ACPClient:
    def __init__(self, home: Path, *, timeout: float = 15):
        self.timeout = timeout
        self.process = None
        self.selector = selectors.DefaultSelector()
        self.buffer = bytearray()
        self.events = []
        self.pending = set()
        self.responses = {}
        self.next_id = 1
        self.total = 0
        self.sent = 0
        self.stderr_bytes = 0
        self.initialized = False
        self.sessions = set()
        self.active_prompt = None
        self.home = home
        try:
            if any(key in os.environ for key in OVERRIDES):
                raise ProtocolError('environment overrides forbidden')
            if not private_home(home):
                raise ProtocolError('private owned home required')
            if EXECUTABLE.is_symlink() or not EXECUTABLE.is_file():
                raise ProtocolError('fixed executable unavailable')
            with EXECUTABLE.open('rb') as binary:
                digest = hashlib.file_digest(binary, 'sha256').hexdigest()
            if digest != EXECUTABLE_SHA256:
                raise ProtocolError('executable digest mismatch')
            self.process = subprocess.Popen(
                [str(EXECUTABLE), 'acp'], cwd=home, env=environment(home),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True, shell=False)
            for pipe, label in ((self.process.stdout, 'stdout'), (self.process.stderr, 'stderr')):
                os.set_blocking(pipe.fileno(), False)
                self.selector.register(pipe, selectors.EVENT_READ, label)
            os.set_blocking(self.process.stdin.fileno(), False)
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self.process is not None:
                try:
                    with suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                except PermissionError:
                    # macOS can deny killpg after the owned child has exited.
                    # A live process is still a cleanup failure, never success.
                    if self.process.poll() is None:
                        self.process.kill()
                        raise ProtocolError('process group cleanup denied') from None
                finally:
                    self.process.wait(timeout=5)
                    for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
                        pipe.close()
        finally:
            self.selector.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def send(self, frame):
        data = json.dumps(frame, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        if len(data) > MAX_FRAME or self.sent + len(data) > MAX_TOTAL:
            raise ProtocolError('write budget exceeded')
        deadline = time.monotonic() + self.timeout
        view = memoryview(data)
        with selectors.DefaultSelector() as writable:
            writable.register(self.process.stdin, selectors.EVENT_WRITE)
            while view:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not writable.select(remaining):
                    raise ProtocolError('write deadline exceeded')
                try:
                    count = os.write(self.process.stdin.fileno(), view)
                    view = view[count:]
                except BlockingIOError:
                    continue
                except OSError:
                    raise ProtocolError('transport write failed') from None
        self.sent += len(data)

    def request(self, method, params):
        if len(self.pending) >= 8:
            raise ProtocolError('pending request limit exceeded')
        request_id = self.next_id
        self.next_id += 1
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        self.pending.add(request_id)
        return request_id

    def notify(self, method, params):
        self.send({'jsonrpc': '2.0', 'method': method, 'params': params})

    def dispatch(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            raise ProtocolError('invalid JSON-RPC envelope')
        if 'method' in message:
            method = message['method']
            if not isinstance(method, str) or not isinstance(message.get('params', {}), dict):
                raise ProtocolError('invalid method envelope')
            if 'id' in message:
                if type(message['id']) not in (int, str):
                    raise ProtocolError('invalid server request id')
                # Never execute tool requests. Cancellation is a normative permission outcome.
                reply = {'jsonrpc': '2.0', 'id': message['id']}
                if method == 'session/request_permission':
                    reply['result'] = {'outcome': {'outcome': 'cancelled'}}
                else:
                    reply['error'] = {'code': -32601, 'message': 'Client capability not supported'}
                self.send(reply)
            if len(self.events) >= MAX_EVENTS:
                raise ProtocolError('event budget exceeded')
            self.events.append(message)
        else:
            request_id = message.get('id')
            if type(request_id) is not int or request_id not in self.pending or request_id in self.responses:
                raise ProtocolError('unmatched or duplicate response id')
            if ('result' in message) == ('error' in message):
                raise ProtocolError('invalid response envelope')
            self.responses[request_id] = message

    def pump(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError('response deadline exceeded')
        ready = self.selector.select(min(remaining, 0.1))
        for key, _ in ready:
            data = os.read(key.fd, 4096)
            if not data:
                self.selector.unregister(key.fileobj)
                if key.data == 'stdout':
                    raise ProtocolError('ACP stdout closed')
                continue
            self.total += len(data)
            if self.total > MAX_TOTAL:
                raise ProtocolError('read budget exceeded')
            if key.data == 'stderr':
                self.stderr_bytes += len(data)  # do not persist untrusted diagnostic contents
                continue
            self.buffer.extend(data)
            while b'\n' in self.buffer:
                line, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                if len(line) > MAX_FRAME:
                    raise ProtocolError('frame budget exceeded')
                try:
                    message = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (ValueError, UnicodeError, RecursionError):
                    raise ProtocolError('invalid JSON frame') from None
                self.dispatch(message)
            if len(self.buffer) > MAX_FRAME:
                raise ProtocolError('frame budget exceeded')

    def wait(self, request_id):
        if request_id not in self.pending:
            raise ProtocolError('unknown pending id')
        deadline = time.monotonic() + self.timeout
        while request_id not in self.responses:
            self.pump(deadline)
        response = self.responses.pop(request_id)
        self.pending.remove(request_id)
        is_prompt = request_id == self.active_prompt
        if is_prompt:
            self.active_prompt = None
        if 'error' in response:
            error = response['error']
            raise RemoteError(error.get('code') if isinstance(error, dict) else None)
        result = response['result']
        if is_prompt:
            if not isinstance(result, dict) or not isinstance(result.get('stopReason'), str):
                raise ProtocolError('invalid prompt result')
            # Protocol completion is NOT business success. Kimi 2.2.0 maps
            # provider failures to end_turn, even after partial text. Only a
            # future trusted, turn-correlated evidence channel can resolve it.
            return {'stopReason': result['stopReason'], 'protocol_status': 'completed',
                    'business_outcome': 'cancelled' if result['stopReason'] == 'cancelled' else 'unknown'}
        return result

    def call(self, method, params):
        return self.wait(self.request(method, params))

    def initialize(self):
        result = self.call('initialize', {'protocolVersion': 1,
            'clientCapabilities': {}, 'clientInfo': {'name': 'havenbox-hb04-probe', 'version': '1'}})
        if not isinstance(result, dict) or result.get('protocolVersion') != 1:
            raise ProtocolError('unsupported negotiated protocol')
        if not isinstance(result.get('agentCapabilities'), dict):
            raise ProtocolError('missing agent capabilities')
        if result.get('agentInfo', {}).get('version') != '2.2.0':
            raise ProtocolError('unexpected agent version')
        self.initialized = True
        return result

    def new_session(self):
        if not self.initialized:
            raise ProtocolError('initialize required')
        result = self.call('session/new', {'cwd': str(self.home), 'mcpServers': []})
        if not isinstance(result, dict) or not isinstance(result.get('sessionId'), str) or not result['sessionId']:
            raise ProtocolError('invalid session result')
        self.sessions.add(result['sessionId'])
        return result

    def prompt(self, session_id, text):
        if session_id not in self.sessions or self.active_prompt is not None:
            raise ProtocolError('session unknown or turn already active')
        self.active_prompt = self.request('session/prompt', {
            'sessionId': session_id, 'prompt': [{'type': 'text', 'text': text}]})
        return self.active_prompt

    def cancel(self, session_id):
        if session_id not in self.sessions:
            raise ProtocolError('unknown session')
        self.notify('session/cancel', {'sessionId': session_id})
