#!/usr/bin/env python3
"""Offline, fail-fast HB-09 build/test/package entry; never installs a toolchain."""
import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.build' / 'engineering'
LOCK = {}  # Loaded inside main's diagnostic exception boundary.


def run(*args, **kwargs):
    print('+', ' '.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), cwd=ROOT, check=True, **kwargs)


def prepare():
    sdk = Path(os.environ.get('SDKROOT', '/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk'))
    settings = plistlib.loads((sdk / 'SDKSettings.plist').read_bytes())
    if settings['Version'] != LOCK['sdkVersion']:
        raise RuntimeError('SDK version differs from config/build-toolchain.json')
    os.environ['SDKROOT'] = str(sdk)
    os.environ['MACOSX_DEPLOYMENT_TARGET'] = LOCK['deploymentTarget']
    swift = os.environ.get('HB_SWIFTC', 'swiftc')
    for command, expected in [(swift, LOCK['swiftVersion']), ('rustc', LOCK['rustVersion']), ('cargo', LOCK['cargoVersion'])]:
        version = run(command, '--version', capture_output=True, text=True).stdout
        if expected not in version.splitlines() and not any(line.endswith(expected) for line in version.splitlines()):
            raise RuntimeError(f'{command}: unpinned toolchain: {version}')
    if sys.version.split()[0] != LOCK['pythonVersion']:
        raise RuntimeError('Python version differs from build-toolchain.json')
    OUT.mkdir(parents=True, exist_ok=True)
    return swift, sdk


def build(swift, sdk):
    common = ['-sdk', sdk, '-target', 'arm64-apple-macosx14.0', '-swift-version', '6', '-warnings-as-errors']
    run(swift, *common, '-emit-library', '-static', '-emit-module', '-module-name', 'HavenboxCore',
        '-emit-module-path', OUT / 'HavenboxCore.swiftmodule',
        *sorted((ROOT / 'Sources/HavenboxCore').glob('*.swift')), '-o', OUT / 'libHavenboxCore.a')
    for name, source, parse in [
        ('Havenbox', 'Sources/HavenboxApp/HavenboxApp.swift', True),
        ('havenbox-helper', 'Sources/HavenboxHelper/main.swift', False),
        ('core-tests', 'tests/HavenboxCoreTests/ProtocolTests.swift', True),
    ]:
        run(swift, *common, *(['-parse-as-library'] if parse else []), '-I', OUT,
            ROOT / source, OUT / 'libHavenboxCore.a', '-o', OUT / name)
    run('cargo', 'build', '--manifest-path', 'guest/Cargo.toml', '--locked', '--offline', '--release')


def checks():
    run(sys.executable, 'scripts/check-baseline.py')
    run(sys.executable, 'scripts/check-development-plan.py')
    run(sys.executable, 'tests/fixtures/csv/check.py')
    run(sys.executable, 'tests/generate-header-vectors.py', '--check')
    run(OUT / 'core-tests')
    run('cargo', 'test', '--manifest-path', 'guest/Cargo.toml', '--locked', '--offline')
    for binary in [OUT / 'havenbox-helper', ROOT / 'guest/target/release/hb-supervisor']:
        run(binary, '--version')
        for args in [[], ['--validation-grant', 'untrusted'], ['--release']]:
            result = subprocess.run([str(binary), *args], capture_output=True, text=True)
            if result.returncode != 78 or 'not implemented' not in result.stderr:
                raise RuntimeError(f'{binary}: expected explicit unavailable exit 78')


def package():
    # Fresh directory only; never remove/replace a previous package or user path.
    destination = ROOT / 'dist' / 'Havenbox-engineering'
    destination.mkdir(parents=True, exist_ok=False)
    app = destination / 'Havenbox.app/Contents'
    (app / 'MacOS').mkdir(parents=True)
    (app / 'Helpers').mkdir()
    shutil.copy2(OUT / 'Havenbox', app / 'MacOS/Havenbox')
    shutil.copy2(OUT / 'havenbox-helper', app / 'Helpers/havenbox-helper')
    (app / 'Info.plist').write_bytes(plistlib.dumps({
        'CFBundleExecutable': 'Havenbox', 'CFBundleIdentifier': 'dev.havenbox.engineering',
        'CFBundleName': 'Havenbox Engineering', 'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': '0.1.0', 'CFBundleVersion': '1',
        'LSMinimumSystemVersion': '14.0',
    }))
    # Host-native Rust binary is a development artifact, NOT a Linux guest image.
    native = destination / 'host-native-rust'
    native.mkdir()
    shutil.copy2(ROOT / 'guest/target/release/hb-supervisor', native / 'hb-supervisor')
    for path in ['config/runtime-manifest.json', 'config/build-toolchain.json', 'guest/Cargo.lock']:
        shutil.copy2(ROOT / path, destination / Path(path).name)
    (destination / 'NOTICE.txt').write_text('Engineering only; no product execution or ValidationRunGrant issuer.\nNo VM, models, harness runtimes, Linux image, signing approval or redistribution qualification.\n')
    hashes = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(destination.rglob('*')) if p.is_file()}
    (destination / 'inventory.json').write_text(json.dumps({'state': 'candidate', 'files': hashes}, indent=2) + '\n')
    print(destination)


def main():
    global LOCK
    try:
        text = (ROOT / 'config/build-toolchain.json').read_text()
        LOCK = json.loads(text)
    except (OSError, ValueError) as error:
        raise RuntimeError(f'Cannot load toolchain lock: {error}') from error
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'check', 'package'])
    args = parser.parse_args()
    swift, sdk = prepare()
    build(swift, sdk)
    if args.action in ['check', 'package']:
        checks()
    if args.action == 'package':
        package()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f'HB-09 failed: {error}', file=sys.stderr)
        sys.exit(1)
