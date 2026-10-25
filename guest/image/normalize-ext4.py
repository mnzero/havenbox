#!/usr/bin/env python3
"""Normalize and verify ext4 inode ownership using a pinned debugfs binary."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import uuid


DIRECTORY = 0o040000
FILE_TYPE = 0o170000


def debugfs(binary: Path, image: Path, command: str, *, writable: bool = False) -> str:
    args = [str(binary)]
    if writable:
        args.append("-w")
    args.extend(["-R", command, str(image)])
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"debugfs failed for {command!r}: {result.stderr.strip()}")
    return result.stdout


def inventory(binary: Path, image: Path) -> dict[int, tuple[int, int, int]]:
    pending = [2]
    visited_directories: set[int] = set()
    inodes: dict[int, tuple[int, int, int]] = {}
    while pending:
        directory = pending.pop()
        if directory in visited_directories:
            continue
        visited_directories.add(directory)
        listing = debugfs(binary, image, f"ls -p -l <{directory}>")
        for line in listing.splitlines():
            if not line.startswith("/"):
                continue
            fields = line.split("/")
            if len(fields) < 7:
                raise RuntimeError(f"unexpected debugfs listing: {line!r}")
            inode, mode, uid, gid, name = fields[1:6]
            if not inode.isdecimal() or not all(value.isdecimal() for value in (uid, gid)):
                raise RuntimeError(f"invalid numeric field in debugfs listing: {line!r}")
            if not mode or any(character not in "01234567" for character in mode):
                raise RuntimeError(f"invalid mode in debugfs listing: {line!r}")
            inode_number = int(inode)
            inode_mode = int(mode, 8)
            identity = (inode_mode, int(uid), int(gid))
            previous = inodes.setdefault(inode_number, identity)
            if previous != identity:
                raise RuntimeError(f"inconsistent metadata for inode {inode_number}")
            if name not in {".", ".."} and inode_mode & FILE_TYPE == DIRECTORY:
                pending.append(inode_number)
    return inodes


def normalize(
    binary: Path,
    image: Path,
    inodes: dict[int, tuple[int, int, int]],
    timestamp: int | None,
) -> None:
    with tempfile.NamedTemporaryFile("w", prefix="hb11-debugfs-", delete=False) as commands:
        command_path = Path(commands.name)
        for inode, (_, uid, gid) in sorted(inodes.items()):
            if uid != 0:
                commands.write(f"set_inode_field <{inode}> uid 0\n")
            if gid != 0:
                commands.write(f"set_inode_field <{inode}> gid 0\n")
        if timestamp is not None:
            # e2fsck 1.47.4 does not honor E2FSPROGS_FAKE_TIME for s_lastcheck.
            commands.write(f"set_super_value lastcheck {timestamp}\n")
    try:
        result = subprocess.run(
            [str(binary), "-w", "-f", str(command_path), str(image)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"debugfs normalization failed: {result.stderr.strip()}")
    finally:
        os.unlink(command_path)


def verify_superblock(image: Path, expected_uuid: uuid.UUID, timestamp: int) -> None:
    with image.open("rb") as filesystem:
        filesystem.seek(1024)
        superblock = filesystem.read(1024)
    if len(superblock) != 1024 or superblock[0x38:0x3A] != b"\x53\xef":
        raise RuntimeError("invalid ext4 superblock")
    actual_uuid = uuid.UUID(bytes=superblock[0x68:0x78])
    if actual_uuid != expected_uuid:
        raise RuntimeError(f"filesystem UUID mismatch: {actual_uuid} != {expected_uuid}")
    hash_seed = uuid.UUID(bytes=superblock[0xEC:0xFC])
    if hash_seed != expected_uuid:
        raise RuntimeError(f"directory hash seed mismatch: {hash_seed} != {expected_uuid}")
    (last_checked,) = struct.unpack_from("<I", superblock, 0x40)
    if last_checked != timestamp:
        raise RuntimeError(f"last-check time mismatch: {last_checked} != {timestamp}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debugfs", type=Path, required=True)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--minimum-inodes", type=int, required=True)
    parser.add_argument("--normalize", action="store_true")
    parser.add_argument("--expected-uuid", type=uuid.UUID)
    parser.add_argument("--timestamp", type=int)
    args = parser.parse_args()
    if (args.expected_uuid is None) != (args.timestamp is None):
        parser.error("--expected-uuid and --timestamp must be provided together")
    if args.timestamp is not None and not 0 <= args.timestamp <= 0xFFFFFFFF:
        parser.error("--timestamp must fit in an unsigned 32-bit ext4 field")
    for path in (args.debugfs, args.image):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"regular non-symlink file required: {path}")
    if not os.access(args.debugfs, os.X_OK):
        raise RuntimeError("debugfs is not executable")
    before = inventory(args.debugfs, args.image)
    if len(before) < args.minimum_inodes:
        raise RuntimeError(f"incomplete filesystem inventory: {len(before)} inodes")
    if args.normalize or args.timestamp is not None:
        normalize(args.debugfs, args.image, before if args.normalize else {}, args.timestamp)
    after = inventory(args.debugfs, args.image)
    if set(before) != set(after):
        raise RuntimeError("inode inventory changed during ownership normalization")
    drift = {inode: identity for inode, identity in after.items() if identity[1:] != (0, 0)}
    if drift:
        raise RuntimeError(f"non-root inode ownership remains: {drift}")
    if args.expected_uuid is not None and args.timestamp is not None:
        verify_superblock(args.image, args.expected_uuid, args.timestamp)
    print(f"verified {len(after)} ext4 inodes owned by 0:0 in {args.image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
