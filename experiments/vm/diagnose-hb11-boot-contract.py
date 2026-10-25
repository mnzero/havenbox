#!/usr/bin/env python3
"""Capture and compare the HB-11 guest boot inventory without changing a candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_NETWORK = ["lo"]
EXPECTED_BLOCKS = [("vda", "253:0"), ("vdb", "253:16")]
BOOT_MOUNTS_RE = re.compile(
    r"const BOOT_MOUNTS:.*?= &\[(.*?)\n\];", re.DOTALL
)
MOUNT_TUPLE_RE = re.compile(
    r"\(\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*,"
    r"\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*,?\s*\)",
    re.DOTALL,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_boot_mounts(source: Path) -> dict[str, tuple[str, str, str, str]]:
    text = source.read_text()
    match = BOOT_MOUNTS_RE.search(text)
    if not match:
        raise ValueError(f"BOOT_MOUNTS not found in {source}")
    mounts = {
        path: (filesystem, mount_source, mount_options, super_options)
        for path, filesystem, mount_source, mount_options, super_options
        in MOUNT_TUPLE_RE.findall(match.group(1))
    }
    if not mounts:
        raise ValueError(f"BOOT_MOUNTS is empty in {source}")
    return mounts


def parse_mountinfo(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        entry: dict[str, Any] = {"line": line_number, "raw": line}
        if " - " not in line:
            differences.append({
                "kind": "mount-line-shape",
                "line": line_number,
                "expected": "one ' - ' separator",
                "actual": line,
            })
            continue
        left, right = line.split(" - ", 1)
        fields = left.split(" ")
        fs = right.split(" ")
        entry.update({"left_fields": fields, "right_fields": fs})
        if len(fields) != 6 or len(fs) != 3:
            differences.append({
                "kind": "mount-field-count",
                "line": line_number,
                "expected": {"left": 6, "right": 3},
                "actual": {"left": len(fields), "right": len(fs)},
            })
            continue
        entry.update({
            "id": fields[0],
            "parent": fields[1],
            "dev": fields[2],
            "root": fields[3],
            "path": fields[4],
            "mount_options": fields[5],
            "filesystem": fs[0],
            "source": fs[1],
            "super_options": fs[2],
        })
        entries.append(entry)
    return entries, differences


def _decimal(value: str) -> bool:
    return bool(re.fullmatch(r"0|[1-9][0-9]*", value)) and int(value) <= 0xFFFFFFFF


def _device(value: str) -> tuple[int, int] | None:
    parts = value.split(":")
    if len(parts) != 2 or not all(_decimal(part) for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def compare_inventory(
    mountinfo: str,
    network_names: list[str],
    block_devices: list[tuple[str, str]],
    expected_mounts: dict[str, tuple[str, str, str, str]],
) -> dict[str, Any]:
    differences: list[dict[str, Any]] = []

    if network_names != EXPECTED_NETWORK:
        differences.append({
            "kind": "network-inventory",
            "expected": EXPECTED_NETWORK,
            "actual": network_names,
        })
    if block_devices != EXPECTED_BLOCKS:
        differences.append({
            "kind": "block-inventory",
            "expected": EXPECTED_BLOCKS,
            "actual": block_devices,
        })

    entries, parse_differences = parse_mountinfo(mountinfo)
    differences.extend(parse_differences)
    seen_ids: dict[int, str] = {}
    seen_devices: dict[tuple[int, int], str] = {}
    seen_paths: dict[str, dict[str, Any]] = {}

    for entry in entries:
        path = entry["path"]
        mount_id = entry["id"]
        parent_id = entry["parent"]
        device = _device(entry["dev"])
        if entry["root"] != "/":
            differences.append({
                "kind": "mount-root",
                "path": path,
                "expected": "/",
                "actual": entry["root"],
            })
        if not _decimal(mount_id) or not _decimal(parent_id):
            differences.append({
                "kind": "mount-id-format",
                "path": path,
                "expected": "canonical u32 decimal id and parent",
                "actual": {"id": mount_id, "parent": parent_id},
            })
        else:
            numeric_id = int(mount_id)
            numeric_parent = int(parent_id)
            if numeric_id == 0 or numeric_id == numeric_parent:
                differences.append({
                    "kind": "mount-id-value",
                    "path": path,
                    "expected": "nonzero id distinct from parent",
                    "actual": {"id": numeric_id, "parent": numeric_parent},
                })
            if numeric_id in seen_ids:
                differences.append({
                    "kind": "duplicate-mount-id",
                    "path": path,
                    "actual": numeric_id,
                    "first_path": seen_ids[numeric_id],
                })
            else:
                seen_ids[numeric_id] = path

        if device is None:
            differences.append({
                "kind": "mount-device-format",
                "path": path,
                "expected": "canonical major:minor u32 pair",
                "actual": entry["dev"],
            })
        elif device in seen_devices:
            differences.append({
                "kind": "duplicate-mount-device",
                "path": path,
                "actual": entry["dev"],
                "first_path": seen_devices[device],
            })
        else:
            seen_devices[device] = path

        if path in seen_paths:
            differences.append({"kind": "duplicate-mount-path", "path": path})
        else:
            seen_paths[path] = entry

        expected = expected_mounts.get(path)
        if expected is None:
            differences.append({"kind": "unexpected-mount", "path": path, "actual": entry})
            continue
        labels = ("filesystem", "source", "mount_options", "super_options")
        for label, expected_value in zip(labels, expected):
            if entry[label] != expected_value:
                differences.append({
                    "kind": f"mount-{label.replace('_', '-')}",
                    "path": path,
                    "expected": expected_value,
                    "actual": entry[label],
                })
        if device is not None:
            valid = (
                device == (253, 0) if path == "/" else
                device == (253, 16) if path == "/data" else
                device[0] == 0 and device[1] != 0
            )
            if not valid:
                differences.append({
                    "kind": "mount-device",
                    "path": path,
                    "expected": "253:0" if path == "/" else
                                "253:16" if path == "/data" else "0:<nonzero>",
                    "actual": entry["dev"],
                })

    for path in expected_mounts:
        if path not in seen_paths:
            differences.append({"kind": "missing-mount", "path": path})

    for path, entry in seen_paths.items():
        if path not in expected_mounts or not _decimal(entry["id"]) or not _decimal(entry["parent"]):
            continue
        mount_id = int(entry["id"])
        parent_id = int(entry["parent"])
        if path == "/":
            if parent_id in seen_ids:
                differences.append({
                    "kind": "root-parent-present",
                    "path": path,
                    "expected": "parent id absent from inventory",
                    "actual": parent_id,
                })
            continue
        parent_path = "/dev" if path == "/dev/pts" else "/sys" if path == "/sys/fs/cgroup" else "/"
        parent = seen_paths.get(parent_path)
        if parent is None or not _decimal(parent["id"]) or parent_id != int(parent["id"]) or mount_id == parent_id:
            differences.append({
                "kind": "mount-parent",
                "path": path,
                "expected_parent_path": parent_path,
                "expected_parent_id": parent["id"] if parent else None,
                "actual_parent_id": parent_id,
            })

    return {
        "accepted": not differences,
        "differences": differences,
        "actual": {
            "network": network_names,
            "blocks": block_devices,
            "mounts": entries,
        },
    }


def parse_inventory(directory: Path) -> tuple[str, list[str], list[tuple[str, str]]]:
    mountinfo = (directory / "mountinfo").read_text()
    network = [line for line in (directory / "network.txt").read_text().splitlines() if line]
    blocks = []
    for line in (directory / "blocks.txt").read_text().splitlines():
        if not line:
            continue
        name, dev = line.split(" ", 1)
        blocks.append((name, dev))
    return mountinfo, network, blocks


def compare_command(args: argparse.Namespace) -> int:
    mounts, network, blocks = parse_inventory(args.inventory)
    result = compare_inventory(mounts, network, blocks, read_boot_mounts(args.source))
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(output)
    print(output, end="")
    return 0 if result["accepted"] else 1


def run_checked(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, **kwargs)


def debugfs_dump(debugfs: Path, image: Path, guest_path: str, host_path: Path) -> None:
    run_checked([str(debugfs), "-R", f"dump {guest_path} {host_path}", str(image)], capture_output=True)
    if not host_path.is_file():
        raise RuntimeError(f"debugfs did not extract {guest_path} from {image}")


def wrapper_script(devtmpfs_remount: bool = False) -> str:
    remount = ""
    if devtmpfs_remount:
        remount = """mount -o remount,nosuid,noexec,size=65536k,nr_inodes=16384,mode=755 devtmpfs /dev 2>\"$directory/remount.stderr\"
remount_status=$?
printf '%s\\n' "$remount_status" >"$directory/remount.status"
printf 'HB11_DIAG_REMOUNT_EXIT status=%s\\n' "$remount_status" >/dev/console
"""
    return """#!/bin/sh
set -u
directory=/data/hb11-candidate19-premise
mkdir -p "$directory"
""" + remount + """cat /proc/self/mountinfo >"$directory/mountinfo"
for entry in /sys/class/net/*; do
    printf '%s\\n' "${entry##*/}"
done | sort >"$directory/network.txt"
for entry in /sys/class/block/*; do
    printf '%s ' "${entry##*/}"
    cat "$entry/dev"
done | sort >"$directory/blocks.txt"
printf 'HB11_DIAG_CAPTURED\\n' >/dev/console
/usr/sbin/hb-supervisor.real --inspect-boot >"$directory/inspect.stdout" 2>"$directory/inspect.stderr"
inspect_status=$?
printf '%s\\n' "$inspect_status" >"$directory/inspect.status"
printf 'HB11_DIAG_INSPECT_EXIT status=%s\\n' "$inspect_status" >/dev/console
/usr/sbin/hb-supervisor.real "$@" >"$directory/runtime.stdout" 2>"$directory/runtime.stderr"
runtime_status=$?
printf '%s\\n' "$runtime_status" >"$directory/runtime.status"
sync
printf 'HB11_DIAG_RUNTIME_EXIT status=%s\\n' "$runtime_status" >/dev/console
exit "$runtime_status"
"""


def write_wrapper(path: Path, devtmpfs_remount: bool = False) -> None:
    path.write_text(wrapper_script(devtmpfs_remount))
    path.chmod(0o755)


def read_remount_diagnostic(inventory: Path) -> dict[str, Any]:
    status_text = (inventory / "remount.status").read_text().strip()
    try:
        status = int(status_text)
    except ValueError as error:
        raise RuntimeError(f"invalid devtmpfs remount status: {status_text!r}") from error
    return {
        "enabled": True,
        "status": status,
        "stderr": (inventory / "remount.stderr").read_text(),
    }


def capture_command(args: argparse.Namespace) -> int:
    image = args.image.resolve()
    data_snapshot = args.data_snapshot.resolve()
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((image / "manifest.json").read_text())
    candidate_paths = [image / name for name in ("Image", "initramfs-hb11", "root.raw", "data.raw")]
    before = {path.name: sha256(path) for path in candidate_paths}
    for path in candidate_paths:
        expected = manifest["files"][path.name]["sha256"]
        if before[path.name] != expected:
            raise RuntimeError(f"candidate hash mismatch for {path.name}")
    snapshot_before = sha256(data_snapshot)

    with tempfile.TemporaryDirectory(prefix="hb11diag-", dir="/tmp") as temporary:
        clone = Path(temporary) / "candidate"
        clone.mkdir()
        for path in candidate_paths:
            run_checked(["cp", "-c", str(path), str(clone / path.name)])
        root_clone = clone / "root.raw"
        data_clone = clone / "data.raw"
        data_clone.unlink()
        run_checked(["cp", "-c", str(data_snapshot), str(data_clone)])
        root_clone.chmod(0o600)
        data_clone.chmod(0o600)

        original = Path(temporary) / "hb-supervisor.original"
        debugfs_dump(args.debugfs, root_clone, "/usr/sbin/hb-supervisor", original)
        if sha256(original) != manifest["files"]["hb-supervisor"]["sha256"]:
            raise RuntimeError("supervisor hash in root clone does not match candidate manifest")
        supervisor_hash = sha256(original)

        wrapper = Path(temporary) / "hb-supervisor-wrapper"
        write_wrapper(wrapper, args.devtmpfs_remount)
        for command in (
            "ln /usr/sbin/hb-supervisor /usr/sbin/hb-supervisor.real",
            "unlink /usr/sbin/hb-supervisor",
            f"write {wrapper} /usr/sbin/hb-supervisor",
            "set_inode_field /usr/sbin/hb-supervisor mode 0100755",
        ):
            run_checked([str(args.debugfs), "-w", "-R", command, str(root_clone)], capture_output=True)
        replaced = Path(temporary) / "hb-supervisor.replaced"
        debugfs_dump(args.debugfs, root_clone, "/usr/sbin/hb-supervisor.real", replaced)
        if sha256(replaced) != sha256(original):
            raise RuntimeError("replaced supervisor hash changed inside root clone")
        fsck = subprocess.run(
            [str(args.e2fsck), "-fy", str(root_clone)], text=True, capture_output=True
        )
        if fsck.returncode not in (0, 1):
            raise RuntimeError(f"e2fsck failed for root clone: {fsck.stderr}")

        socket_dir = Path(temporary) / "sockets"
        socket_dir.mkdir()
        command = [
            str(args.qemu), "-M", "virt", "-accel", "hvf", "-cpu", "host",
            "-smp", "2", "-m", "4096M", "-nodefaults", "-nographic",
            "-nic", "none", "-serial", "stdio", "-monitor", "none",
            "-kernel", str(clone / "Image"), "-initrd", str(clone / "initramfs-hb11"),
            "-append", "console=ttyAMA0 root=/dev/vda ro init=/sbin/hb-init "
                       "hb.instance=00112233445566778899aabbccddeeff hb.epoch=1 "
                       "hb.synthetic_role=harness hb.synthetic_case=normal",
            "-drive", f"if=virtio,format=raw,file={root_clone},readonly=on",
            "-drive", f"if=virtio,format=raw,file={data_clone},snapshot=off",
            "-device", "virtio-serial-pci,id=hbserial",
        ]
        for name in ("control", "file", "inference"):
            socket = socket_dir / f"{name}.sock"
            command.extend([
                "-chardev", f"socket,id={name},path={socket},server=on,wait=off",
                "-device", f"virtserialport,bus=hbserial.0,chardev={name},name=hb.{name}",
            ])
        environment = os.environ.copy()
        environment["HB02_QEMU_SYSTEM_FRAMEWORK"] = str(args.framework)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
        try:
            serial, _ = process.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                serial, _ = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                serial, _ = process.communicate()
        (state / "qemu.log").write_text(serial)

        inventory = state / "inventory"
        inventory.mkdir(exist_ok=True)
        inventory_names = [
            "mountinfo", "network.txt", "blocks.txt", "inspect.stdout",
            "inspect.stderr", "inspect.status", "runtime.stdout", "runtime.stderr",
            "runtime.status",
        ]
        if args.devtmpfs_remount:
            inventory_names.extend(("remount.status", "remount.stderr"))
        for name in inventory_names:
            destination = inventory / name
            if destination.exists():
                destination.unlink()
            debugfs_dump(args.debugfs, data_clone, f"/hb11-candidate19-premise/{name}", destination)

    after = {path.name: sha256(path) for path in candidate_paths}
    snapshot_after = sha256(data_snapshot)
    if after != before or snapshot_after != snapshot_before:
        raise RuntimeError("candidate or runtime data snapshot changed during diagnostic capture")

    mounts, network, blocks = parse_inventory(state / "inventory")
    comparison = compare_inventory(mounts, network, blocks, read_boot_mounts(args.source))
    (state / "comparison.json").write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n")
    remount_diagnostic = (
        read_remount_diagnostic(state / "inventory")
        if args.devtmpfs_remount else {"enabled": False}
    )
    receipt = {
        "schemaVersion": "havenbox.hb11-candidate19-premise.v1",
        "candidate": str(image.relative_to(ROOT)),
        "runtimeDataSnapshot": str(data_snapshot.relative_to(ROOT)),
        "candidateHashesBefore": before,
        "candidateHashesAfter": after,
        "runtimeDataSnapshotSha256Before": snapshot_before,
        "runtimeDataSnapshotSha256After": snapshot_after,
        "supervisorReplacementSha256": supervisor_hash,
        "inventoryAccepted": comparison["accepted"],
        "differenceCount": len(comparison["differences"]),
        "inspectStatus": int((state / "inventory/inspect.status").read_text().strip()),
        "runtimeStatus": int((state / "inventory/runtime.status").read_text().strip()),
        "devtmpfsRemount": remount_diagnostic,
        "qemuCommand": command,
        "temporaryCloneRemoved": True,
    }
    (state / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"receipt": receipt, "comparison": comparison}, indent=2, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    compare = subparsers.add_parser("compare", help="compare a captured inventory with Rust expectations")
    compare.add_argument("inventory", type=Path)
    compare.add_argument("--source", type=Path, default=ROOT / "guest/supervisor/src/lib.rs")
    compare.add_argument("--output", type=Path)
    compare.set_defaults(function=compare_command)

    capture = subparsers.add_parser("capture", help="capture from a temporary clone, then compare")
    capture.add_argument("--image", type=Path, required=True)
    capture.add_argument("--data-snapshot", type=Path, required=True)
    capture.add_argument("--state-dir", type=Path, required=True)
    capture.add_argument("--source", type=Path, default=ROOT / "guest/supervisor/src/lib.rs")
    capture.add_argument("--qemu", type=Path, default=ROOT / "experiments/vm/tools/qemu-system-aarch64")
    capture.add_argument("--framework", type=Path, default=ROOT / "experiments/vm/tools/qemu-aarch64-softmmu")
    capture.add_argument("--debugfs", type=Path, default=ROOT / "experiments/vm/tools/debugfs")
    capture.add_argument("--e2fsck", type=Path, default=ROOT / "experiments/vm/tools/e2fsck")
    capture.add_argument("--timeout", type=float, default=12.0)
    capture.add_argument(
        "--devtmpfs-remount",
        action="store_true",
        help="remount devtmpfs with the fixed boot-contract super options before capture",
    )
    capture.set_defaults(function=capture_command)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
