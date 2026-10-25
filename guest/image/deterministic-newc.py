#!/usr/bin/env python3
"""Write a deterministic, initramfs-compatible ``newc`` archive to stdout."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import sys
from typing import BinaryIO, Hashable


NEWC_MAGIC = b"070701"
TRAILER = b"TRAILER!!!"


@dataclass(frozen=True)
class Entry:
    name: bytes
    source: bytes
    metadata: os.stat_result


def _entries(root: Path) -> list[Entry]:
    root_bytes = os.fsencode(os.fspath(root))
    root_metadata = os.lstat(root_bytes)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError(f"archive root must be a directory: {root}")

    entries = [Entry(b".", root_bytes, root_metadata)]
    pending: list[tuple[bytes, bytes]] = [(b".", root_bytes)]
    while pending:
        archive_directory, source_directory = pending.pop()
        with os.scandir(source_directory) as directory:
            children = sorted(directory, key=lambda child: child.name)
        for child in children:
            name = archive_directory + b"/" + child.name
            source = os.path.join(source_directory, child.name)
            metadata = child.stat(follow_symlinks=False)
            mode = metadata.st_mode
            if not any(
                predicate(mode)
                for predicate in (stat.S_ISDIR, stat.S_ISREG, stat.S_ISLNK)
            ):
                raise ValueError(
                    f"unsupported archive entry type: {os.fsdecode(source)}"
                )
            entries.append(Entry(name, source, metadata))
            if stat.S_ISDIR(mode):
                pending.append((name, source))
    return sorted(entries, key=lambda entry: entry.name)


def _padding(size: int, alignment: int) -> bytes:
    return b"\0" * (-size % alignment)


def _write_record(
    output: BinaryIO,
    *,
    inode: int,
    mode: int,
    links: int,
    mtime: int,
    name: bytes,
    data: bytes,
) -> int:
    values = (
        inode,
        mode,
        0,  # uid: initramfs contents are always owned by root.
        0,  # gid
        links,
        mtime,
        len(data),
        0,  # devmajor: archive-local inode numbers define hard-link groups.
        0,  # devminor
        0,  # rdevmajor
        0,  # rdevminor
        len(name) + 1,
        0,  # check: the newc format does not carry a data checksum.
    )
    if any(value < 0 or value > 0xFFFFFFFF for value in values):
        raise ValueError(f"newc field does not fit in 32 bits for {name!r}")
    header = NEWC_MAGIC + b"".join(f"{value:08x}".encode("ascii") for value in values)
    name_record = header + name + b"\0"
    name_padding = _padding(len(name_record), 4)
    data_padding = _padding(len(data), 4)
    output.write(name_record)
    output.write(name_padding)
    output.write(data)
    output.write(data_padding)
    return len(name_record) + len(name_padding) + len(data) + len(data_padding)


def _read_file(path: bytes) -> bytes:
    with open(path, "rb") as source:
        return source.read()


def write_archive(root: Path, output: BinaryIO, *, mtime: int) -> None:
    entries = _entries(root)
    regular_groups: dict[tuple[int, int], list[Entry]] = {}
    directory_links = {entry.name: 2 for entry in entries if stat.S_ISDIR(entry.metadata.st_mode)}
    for entry in entries:
        if stat.S_ISREG(entry.metadata.st_mode):
            key = (entry.metadata.st_dev, entry.metadata.st_ino)
            regular_groups.setdefault(key, []).append(entry)
        elif stat.S_ISDIR(entry.metadata.st_mode) and entry.name != b".":
            parent = entry.name.rsplit(b"/", 1)[0]
            directory_links[parent] += 1

    group_inode: dict[Hashable, int] = {}
    total_size = 0
    for entry in entries:
        mode = entry.metadata.st_mode
        if stat.S_ISREG(mode):
            group: Hashable = ("regular", entry.metadata.st_dev, entry.metadata.st_ino)
            members = regular_groups[(entry.metadata.st_dev, entry.metadata.st_ino)]
            links = len(members)
            data = _read_file(entry.source) if entry is members[-1] else b""
        elif stat.S_ISDIR(mode):
            group = ("path", entry.name)
            links = directory_links[entry.name]
            data = b""
        else:
            group = ("path", entry.name)
            links = 1
            data = os.readlink(entry.source)
            if isinstance(data, str):
                data = os.fsencode(data)

        inode = group_inode.setdefault(group, len(group_inode) + 1)
        total_size += _write_record(
            output,
            inode=inode,
            mode=mode,
            links=links,
            mtime=mtime,
            name=entry.name,
            data=data,
        )

    total_size += _write_record(
        output,
        inode=0,
        mode=0,
        links=1,
        mtime=mtime,
        name=TRAILER,
        data=b"",
    )
    output.write(_padding(total_size, 512))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mtime", type=int, required=True)
    args = parser.parse_args()
    if args.mtime < 0 or args.mtime > 0xFFFFFFFF:
        parser.error("--mtime must fit in an unsigned 32-bit newc field")
    write_archive(args.root, sys.stdout.buffer, mtime=args.mtime)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
