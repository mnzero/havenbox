#!/usr/bin/env python3
"""Offline main-chain regression gate. Exit 0=covered checks pass, 1=failed, 2=blocked.
No downloads, installation, product acceptance updates, or historical process cleanup.
"""
import argparse
import contextlib
import datetime
import ipaddress
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SandboxUnavailable(Exception):
    """Stop checks while retaining the gate failure through cleanup."""


def sandbox_profile(loopback):
    # Seatbelt's localhost includes en0; literal IP filters are unsupported on
    # this host. Never broaden permissions to approximate a loopback exception.
    return '(version 1)(allow default)(deny network*)'


def resolved_loopback(host):
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})
    if not addresses:
        raise ValueError('no resolved loopback addresses')
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if not parsed.is_loopback or getattr(parsed, 'ipv4_mapped', None) is not None:
            raise ValueError(f'non-loopback or mapped address rejected: {address}')
    return addresses


def classify_pi(entry, log):
    if entry['status'] != 'pass':
        return
    try:
        evidence = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        if not evidence or any(not isinstance(item, dict) or 'status' not in item for item in evidence):
            raise ValueError('missing structured check status')
    except (OSError, ValueError) as error:
        entry.update(status='blocked', reason={'code': 'invalid_pi_evidence', 'detail': str(error)})
        return
    incomplete = [item for item in evidence if item['status'] != 'pass']
    if incomplete:
        entry.update(status='fail' if any(item['status'] == 'fail' for item in incomplete) else 'blocked',
                     reason={'code': 'pi_incomplete_checks', 'checks': incomplete})


def classify_regression(entry, log):
    """A skipped real-process startup is missing evidence, never a green gate."""
    if entry['status'] != 'pass':
        return
    try:
        blocked = [json.loads(line.partition('HB_TEST_BLOCKED ')[2])
                   for line in log.read_text().splitlines() if 'HB_TEST_BLOCKED ' in line]
        if blocked:
            entry.update(status='blocked', reason={'code': 'regression_startup_blocked', 'checks': blocked})
    except (OSError, ValueError) as error:
        entry.update(status='blocked', reason={'code': 'invalid_regression_evidence', 'detail': str(error)})


def main(argv=None, sandbox_command=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-dir', type=Path, required=True, help='new directory; never overwritten')
    parser.add_argument('--loopback', action='store_true', help='request loopback fixtures; blocked until exact-address sandbox filtering is available')
    parser.add_argument('--host-process-tests', action='store_true', help='explicitly run network-free lifecycle code outside Seatbelt (setuid ps is forbidden inside it)')
    args = parser.parse_args(argv)
    report = args.report_dir.resolve()
    report.mkdir(parents=True, exist_ok=False)
    records = []
    scratch = Path(tempfile.mkdtemp(prefix='havenbox-tests-')).resolve()
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(scratch / 'home'),
           'TMPDIR': str(scratch / 'tmp'), 'LANG': 'en_US.UTF-8', 'TZ': 'UTC',
           'PYTHONHASHSEED': '0', 'PYTHONDONTWRITEBYTECODE': '1',
           'CARGO_NET_OFFLINE': 'true', 'CARGO_TARGET_DIR': str(scratch / 'cargo'),
           'HB_BUILD_ROOT': str(scratch / 'build'), 'CLANG_MODULE_CACHE_PATH': str(scratch / 'clang-cache'),
           'SWIFT_MODULECACHE_PATH': str(scratch / 'swift-cache')}
    for key in ('HOME', 'TMPDIR'):
        Path(env[key]).mkdir(mode=0o700)
    for key in ('SDKROOT', 'HB_SWIFTC'):
        if key in os.environ:
            env[key] = os.environ[key]
    profile = sandbox_profile(args.loopback)
    prefix = ['/usr/bin/sandbox-exec', '-p', profile]

    def run(name, command, timeout=180, blocked_codes=(), sandbox=True):
        start = time.monotonic()
        log = report / (name + '.log')
        entry = {'name': name, 'command': list(map(str, command)), 'cwd': str(ROOT), 'log': log.name,
                 'sandbox': sandbox}
        records.append(entry)
        try:
            with log.open('wb') as output:
                process = subprocess.Popen((prefix if sandbox else []) + list(map(str, command)), cwd=ROOT, env=env,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = process.wait(timeout=timeout)
                    entry.update(exit_code=code, status='pass' if code == 0 else 'blocked' if code in blocked_codes else 'fail')
                except subprocess.TimeoutExpired:
                    entry.update(exit_code=None, status='fail', reason=f'timeout after {timeout}s')
                finally:
                    # Only this child's dedicated group; never pgrep/kill by name.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
                    entry['child_reaped'] = True
        except OSError as error:
            entry.update(status='blocked', exit_code=None, reason=str(error))
        entry['seconds'] = round(time.monotonic() - start, 3)
        if name == 'pi-real-rpc':
            classify_pi(entry, log)
        if name == 'dsh-regression':
            classify_regression(entry, log)
        print(name, entry['status'], entry.get('exit_code'), flush=True)
        return entry

    try:
        snapshot = {'platform': platform.platform(), 'machine': platform.machine(),
                    'python': sys.version, 'python_executable': sys.executable,
                    'environment': env, 'sandbox_profile': profile,
                    'started': datetime.datetime.now(datetime.UTC).isoformat()}
        (report / 'environment.json').write_text(json.dumps(snapshot, indent=2) + '\n')
        gate = run('sandbox', sandbox_command or ['/usr/bin/true'])
        if gate['status'] != 'pass':
            raise SandboxUnavailable()
        if args.loopback:
            records.append({'name': 'loopback-policy', 'status': 'blocked', 'exit_code': None,
                            'reason': {'code': 'exact_loopback_filter_unavailable',
                                       'detail': 'Seatbelt localhost includes non-loopback interfaces; network remains denied; harnesses not started.'}})
            raise SandboxUnavailable()
        for name, command in [('swift-version', [env.get('HB_SWIFTC', 'swiftc'), '--version']),
                              ('rust-version', ['rustc', '--version']), ('cargo-version', ['cargo', '--version'])]:
            run(name, command)
        run('engineering', [sys.executable, 'scripts/engineering.py', 'check'], timeout=600)
        for name, directory in [('contracts', 'tests/contracts'),
                                ('kimi-regression', 'experiments/harness/kimi'), ('pi-regression', 'experiments/harness/pi'),
                                ('dsh-regression', 'experiments/harness/dsh'), ('model-regression', 'experiments/local-model')]:
            run(name, [sys.executable, '-m', 'unittest', 'discover', '-s', directory, '-p', 'test_*.py', '-v'])
        if args.host_process_tests:
            run('vm-host-regression', [sys.executable, '-m', 'unittest', 'discover', '-s', 'experiments/vm', '-v'], sandbox=False)
            run('process-lifecycle', [sys.executable, 'experiments/process-lifecycle/probe.py', '--probe'], timeout=90, sandbox=False)
            run('process-regression', [sys.executable, '-m', 'unittest', 'discover', '-s', 'experiments/process-lifecycle', '-v'], sandbox=False)
        else:
            records.append({'name': 'host-process-tests', 'status': 'blocked', 'exit_code': None,
                            'reason': 'setuid ps cannot execute inside Seatbelt; opt in with --host-process-tests'})
        run('vm-assets', ['bash', 'experiments/vm/replay-hb02-local.sh', 'verify'], blocked_codes=(3,))
        run('model-preflight', [sys.executable, 'experiments/local-model/probe.py', '--offline'], blocked_codes=(2,))
        run('pi-real-rpc', [sys.executable, 'experiments/harness/pi/probe.py'], blocked_codes=(2,))
        run('dsh-real-rpc', [sys.executable, 'experiments/harness/dsh/probe.py'], blocked_codes=(3,))
        records.append({'name': 'kimi-real-acp', 'status': 'blocked', 'exit_code': None,
                        'reason': 'requires exact-address loopback isolation; unavailable on this host'})
        for name, reason in {
            'HB02-HB11-real-VM': 'Run reviewed VM runbooks with verified local tools/images; host regression is not VM evidence.',
            'HB03-launchd': 'Mechanism probe only; requires HB11 real RuntimeManager/launchd/helper/QEMU run.',
            'Keychain': 'No isolated real-Keychain test exists; HOME does not isolate macOS Keychain. Do not use production service/account.',
            'HB04-HB07-model-acceptance': 'Metadata/RPC/local fixtures are not authorized real-model or guest-harness acceptance.'
        }.items():
            records.append({'name': name, 'status': 'blocked', 'reason': reason, 'exit_code': None})
    except SandboxUnavailable:
        pass  # Gate record retains the real failure; do not run unconfined checks.
    finally:
        # Read-only residual observation outside Seatbelt (macOS ps is setuid).
        observation = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,command='], capture_output=True, text=True)
        residual = [line.strip() for line in observation.stdout.splitlines() if str(scratch) in line]
        if residual or observation.returncode:
            records.append({'name': 'cleanup-observation', 'status': 'fail', 'exit_code': observation.returncode,
                            'residual': residual})
        shutil.rmtree(scratch)
        result = {'checks': records, 'scratch_removed': not scratch.exists(),
                  'residual_scratch_processes': residual, 'process_observation_exit': observation.returncode,
                  'network': 'denied-by-sandbox',
                  'loopback_requested': args.loopback,
                  'host_process_tests_outside_sandbox': args.host_process_tests,
                  'scope': 'regression-and-preflight-not-product-acceptance',
                  'exit_code': 1 if any(r['status'] == 'fail' for r in records) else 2 if any(r['status'] == 'blocked' for r in records) else 0}
        (report / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    return result['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
