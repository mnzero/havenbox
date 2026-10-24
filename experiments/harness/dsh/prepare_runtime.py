#!/usr/bin/env python3
"""Explicit provisioning only; never invoked by probe or product startup.

macOS arm64. Downloads official Node and npm artifacts into ignored runtime/.
No global npm config, user credentials, lifecycle scripts, or model requests.
"""
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
NODE = "node-v24.9.0-darwin-arm64"
NODE_SHA256 = "961024296c2a8e60daed0784f8b61e0fab5c51d197502a92eff052c72b53209b"
COMMIT = "1f81c047fc48fa190a683e469daa699857e9afe8"
LAUNCHER = '''#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$ROOT/node-v24.9.0-darwin-arm64/bin/node" "$ROOT/node_modules/@deepseek-ai/dsh/lib/bin.js" "$@"
'''


def main():
    if os.uname().sysname != "Darwin" or os.uname().machine != "arm64":
        raise SystemExit("Only the verified darwin-arm64 artifact is supported")
    RUNTIME.mkdir(exist_ok=True)
    home = RUNTIME / "install-home"
    home.mkdir(exist_ok=True)
    for name in ("user.npmrc", "global.npmrc"):
        (home / name).write_text("")
    env = {"HOME": str(home), "PATH": f"{RUNTIME / NODE / 'bin'}:/usr/bin:/bin",
           "npm_config_userconfig": str(home / "user.npmrc"),
           "npm_config_globalconfig": str(home / "global.npmrc"),
           "npm_config_cache": str(RUNTIME / "npm-cache")}
    archive = RUNTIME / "node.tar.gz"
    if not archive.exists():
        urllib.request.urlretrieve("https://nodejs.org/dist/v24.9.0/node-v24.9.0-darwin-arm64.tar.gz", archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != NODE_SHA256:
        raise SystemExit("Node archive hash mismatch")
    if not (RUNTIME / NODE).exists():
        subprocess.run(["/usr/bin/tar", "-xzf", str(archive), "-C", str(RUNTIME)], check=True)
    source = RUNTIME / "source"
    if not source.exists():
        subprocess.run(["/usr/bin/git", "clone", "--depth", "1", "--branch", "dsh-v0.2.1-rc.3",
                        "https://github.com/deepseek-ai/deepseek-harness.git", str(source)], check=True, env=env)
    commit = subprocess.check_output(["/usr/bin/git", "-C", str(source), "rev-parse", "HEAD"], env=env).decode().strip()
    if commit != COMMIT:
        raise SystemExit("Source release commit mismatch")
    (RUNTIME / "package.json").write_text(json.dumps({"private": True, "dependencies": {"@deepseek-ai/dsh": "0.2.1-rc.3"}}) + "\n")
    shutil.copyfile(ROOT / "package-lock.json", RUNTIME / "package-lock.json")
    subprocess.run([str(RUNTIME / NODE / "bin/npm"), "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                    "--registry=https://registry.npmjs.org"], cwd=RUNTIME, env=env, check=True)
    (RUNTIME / "bin").mkdir(exist_ok=True)
    launcher = RUNTIME / "bin/dsh"
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o700)
    # Hash approval is intentionally separate: provisioning cannot rewrite the manifest.
    print("Provisioned; run probe.py to verify against the reviewed manifest")


if __name__ == "__main__":
    main()
