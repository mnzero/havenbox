#!/usr/bin/env python3
"""Regression tests for deterministic HB-11 initramfs and ext4 construction."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
NEWC_WRITER = REPOSITORY / "guest/image/deterministic-newc.py"
NORMALIZER = REPOSITORY / "guest/image/normalize-ext4.py"
TOOLS = REPOSITORY / "experiments/vm/tools"
MKE2FS = TOOLS / "mke2fs"
MKE2FS_CONFIG = TOOLS / "mke2fs.conf"
DEBUGFS = TOOLS / "debugfs"
E2FSCK = TOOLS / "e2fsck"
IMAGE_EPOCH = 1_792_800_000  # 2026-10-24T00:00:00Z
ROOT_UUID = "b0e87f5b-28ba-5ca5-ba8d-2a0e2c18bdc6"
DATA_UUID = "50718f8a-aea7-55d6-a82a-ddd4cedd8e5d"


def create_tree(root: Path, *, reverse: bool) -> None:
    root.chmod(0o755)
    directories = [root / "dir", root / "empty"]
    for directory in reversed(directories) if reverse else directories:
        directory.mkdir()
        directory.chmod(0o755)
    payload = root / "dir/alpha"
    payload.write_bytes(b"deterministic payload\n")
    payload.chmod(0o640)
    os.link(payload, root / "dir/omega")
    os.symlink("alpha", root / "dir/link")
    source_time = IMAGE_EPOCH + (300 if reverse else 100)
    for path in (root, *directories, payload, root / "dir/omega"):
        os.utime(path, (source_time, source_time), follow_symlinks=False)
    os.utime(root / "dir/link", (source_time, source_time), follow_symlinks=False)


def write_newc(root: Path) -> bytes:
    result = subprocess.run(
        ["python3", str(NEWC_WRITER), "--root", str(root), "--mtime", str(IMAGE_EPOCH)],
        check=True,
        capture_output=True,
    )
    return result.stdout


def parse_newc(archive: bytes) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    offset = 0
    while True:
        header = archive[offset : offset + 110]
        if len(header) != 110 or header[:6] != b"070701":
            raise AssertionError(f"invalid newc header at offset {offset}")
        fields = [int(header[index : index + 8], 16) for index in range(6, 110, 8)]
        (
            inode,
            mode,
            uid,
            gid,
            links,
            mtime,
            size,
            _devmajor,
            _devminor,
            _rdevmajor,
            _rdevminor,
            name_size,
            _check,
        ) = fields
        name_start = offset + 110
        name_field = archive[name_start : name_start + name_size]
        if not name_field.endswith(b"\0"):
            raise AssertionError("newc name is not NUL-terminated")
        name = name_field[:-1]
        data_start = (name_start + name_size + 3) & ~3
        data = archive[data_start : data_start + size]
        records.append(
            {
                "name": name,
                "inode": inode,
                "mode": mode,
                "uid": uid,
                "gid": gid,
                "links": links,
                "mtime": mtime,
                "data": data,
            }
        )
        offset = (data_start + size + 3) & ~3
        if name == b"TRAILER!!!":
            break
    if len(archive) % 512 != 0 or any(archive[offset:]):
        raise AssertionError("newc trailer padding is not zero-filled to 512 bytes")
    return records


class DeterministicNewcTests(unittest.TestCase):
    def test_different_source_paths_and_inodes_produce_identical_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hb-newc-a-") as first_directory, tempfile.TemporaryDirectory(
            prefix="hb-newc-b-"
        ) as second_directory:
            first = Path(first_directory)
            second = Path(second_directory)
            create_tree(first, reverse=False)
            create_tree(second, reverse=True)
            self.assertNotEqual((first / "dir/alpha").stat().st_ino, (second / "dir/alpha").stat().st_ino)

            first_archive = write_newc(first)
            second_archive = write_newc(second)
            self.assertEqual(first_archive, second_archive)

            records = parse_newc(first_archive)
            self.assertEqual(
                [record["name"] for record in records],
                [b".", b"./dir", b"./dir/alpha", b"./dir/link", b"./dir/omega", b"./empty", b"TRAILER!!!"],
            )
            entries = {record["name"]: record for record in records}
            for record in records[:-1]:
                self.assertEqual((record["uid"], record["gid"], record["mtime"]), (0, 0, IMAGE_EPOCH))
            self.assertTrue(stat.S_ISDIR(int(entries[b"."]["mode"])))
            self.assertEqual(entries[b"."]["links"], 4)
            self.assertTrue(stat.S_ISLNK(int(entries[b"./dir/link"]["mode"])))
            self.assertEqual(entries[b"./dir/link"]["data"], b"alpha")
            self.assertEqual(entries[b"./dir/alpha"]["inode"], entries[b"./dir/omega"]["inode"])
            self.assertEqual(entries[b"./dir/alpha"]["links"], 2)
            self.assertEqual(entries[b"./dir/alpha"]["data"], b"")
            self.assertEqual(entries[b"./dir/omega"]["data"], b"deterministic payload\n")

            cpio = shutil.which("cpio")
            if cpio:
                listing = subprocess.run(
                    [cpio, "-it"], input=first_archive, check=False, capture_output=True
                )
                self.assertEqual(listing.returncode, 0, listing.stderr.decode(errors="replace"))
                self.assertIn(b"./dir/link", listing.stdout)


@unittest.skipUnless(
    all(path.is_file() for path in (MKE2FS, MKE2FS_CONFIG, DEBUGFS, E2FSCK)),
    "pinned e2fsprogs tools are not available",
)
class DeterministicExt4Tests(unittest.TestCase):
    def run_command(self, arguments: list[str], *, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[bytes]:
        environment = os.environ.copy()
        environment.update(
            {
                "E2FSPROGS_FAKE_TIME": str(IMAGE_EPOCH),
                "SOURCE_DATE_EPOCH": str(IMAGE_EPOCH),
                "MKE2FS_CONFIG": str(MKE2FS_CONFIG),
            }
        )
        result = subprocess.run(arguments, check=False, capture_output=True, env=environment)
        self.assertIn(result.returncode, allowed, result.stderr.decode(errors="replace"))
        return result

    def make_filesystems(self, directory: Path, *, reverse: bool) -> tuple[Path, Path]:
        source = directory / "source"
        source.mkdir()
        create_tree(source, reverse=reverse)
        root_image = directory / "root.raw"
        data_image = directory / "data.raw"
        common = "lazy_itable_init=0,lazy_journal_init=0,nodiscard,root_owner=0:0"
        self.run_command(
            [
                str(MKE2FS),
                "-q",
                "-t",
                "ext4",
                "-L",
                "hb-test-root",
                "-U",
                ROOT_UUID,
                "-E",
                f"hash_seed={ROOT_UUID},{common}",
                "-d",
                str(source),
                str(root_image),
                "16M",
            ]
        )
        self.run_command(
            [
                str(MKE2FS),
                "-q",
                "-t",
                "ext4",
                "-O",
                "project,quota",
                "-E",
                f"quotatype=prjquota,hash_seed={DATA_UUID},{common}",
                "-L",
                "hb-test-data",
                "-U",
                DATA_UUID,
                str(data_image),
                "16M",
            ]
        )
        for image, minimum_inodes in ((root_image, 7), (data_image, 2)):
            self.run_command([str(E2FSCK), "-f", "-y", str(image)], allowed=(0, 1))
            self.run_command(
                [
                    "python3",
                    str(NORMALIZER),
                    "--debugfs",
                    str(DEBUGFS),
                    "--image",
                    str(image),
                    "--minimum-inodes",
                    str(minimum_inodes),
                    "--normalize",
                    "--expected-uuid",
                    ROOT_UUID if image == root_image else DATA_UUID,
                    "--timestamp",
                    str(IMAGE_EPOCH),
                ]
            )
            self.run_command([str(E2FSCK), "-f", "-n", str(image)])
        return root_image, data_image

    def test_pinned_e2fsprogs_double_build_is_byte_identical_and_clean(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hb-ext4-test-") as temporary_directory:
            temporary = Path(temporary_directory)
            first_directory = temporary / "first"
            second_directory = temporary / "second"
            first_directory.mkdir()
            second_directory.mkdir()
            first_root, first_data = self.make_filesystems(first_directory, reverse=False)
            second_root, second_data = self.make_filesystems(second_directory, reverse=True)
            self.assertEqual(first_root.read_bytes(), second_root.read_bytes())
            self.assertEqual(first_data.read_bytes(), second_data.read_bytes())
            self.assertNotEqual(first_root.read_bytes(), first_data.read_bytes())

            for image, expected_uuid in ((first_root, ROOT_UUID), (first_data, DATA_UUID)):
                stats = self.run_command([str(DEBUGFS), "-R", "stats", str(image)]).stdout.decode()
                self.assertIn(f"Filesystem UUID:          {expected_uuid}", stats)
                self.assertIn(f"Directory Hash Seed:      {expected_uuid}", stats)


if __name__ == "__main__":
    unittest.main()
