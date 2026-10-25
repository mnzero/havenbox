#!/usr/bin/env python3
"""Contract tests for guest image mounts and init lifecycle handling."""

from __future__ import annotations

from pathlib import Path
import shlex
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
INIT_SCRIPTS = (
    REPOSITORY / "guest/image/initramfs-init",
    REPOSITORY / "guest/image/hb-init",
)
EXPECTED_OPTIONS = ("nosuid", "noexec", "size=65536k", "nr_inodes=16384", "mode=755")


def devtmpfs_mounts(script: Path) -> list[tuple[int, tuple[str, ...]]]:
    commands = [
        (line_number, tuple(shlex.split(line)))
        for line_number, line in enumerate(script.read_text().splitlines(), start=1)
        if line.startswith("mount ") and " devtmpfs " in f" {line} "
    ]
    if len(commands) != 2:
        raise AssertionError(f"expected two devtmpfs mounts in {script}, found {commands!r}")
    return commands


def files_mode_branch(script: Path) -> list[str]:
    lines = script.read_text().splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == "files)")
    end = next(
        index for index in range(start + 1, len(lines)) if lines[index].strip() == ";;"
    )
    return [line.strip() for line in lines[start : end + 1] if line.strip()]


class GuestImageMountContractTests(unittest.TestCase):
    def test_both_boot_stages_use_the_frozen_devtmpfs_options(self) -> None:
        initial_mount = (
            "mount",
            "-t",
            "devtmpfs",
            "-o",
            ",".join(EXPECTED_OPTIONS),
            "devtmpfs",
            "/dev",
        )
        remount = (
            "mount",
            "-o",
            ",".join(("remount", *EXPECTED_OPTIONS)),
            "devtmpfs",
            "/dev",
        )

        for script in INIT_SCRIPTS:
            with self.subTest(script=script.relative_to(REPOSITORY)):
                mounts = devtmpfs_mounts(script)
                self.assertEqual([command for _, command in mounts], [initial_mount, remount])
                self.assertEqual(mounts[1][0], mounts[0][0] + 1)

    def test_files_mode_reaps_supervisor_reports_status_and_keeps_init_alive(self) -> None:
        script = REPOSITORY / "guest/image/hb-init"
        self.assertEqual(
            files_mode_branch(script),
            [
                "files)",
                'if /usr/sbin/hb-supervisor --run-files "$instance" "$epoch"; then',
                "supervisor_status=0",
                "else",
                "supervisor_status=$?",
                "fi",
                "printf 'MVP02_SUPERVISOR_EXIT status=%s\\n' \"$supervisor_status\" >/dev/console",
                "while :; do",
                "sleep 60 &",
                "idle_pid=$!",
                'wait "$idle_pid" || :',
                "done",
                ";;",
            ],
        )

    def test_only_synthetic_mode_execs_supervisor(self) -> None:
        script = REPOSITORY / "guest/image/hb-init"
        lines = script.read_text().splitlines()
        commands = [
            (line_number, tuple(shlex.split(line.strip())))
            for line_number, line in enumerate(lines, start=1)
            if line.strip().startswith("exec ")
        ]
        self.assertEqual(len(commands), 1)
        line_number, command = commands[0]
        synthetic_line = next(
            index for index, line in enumerate(lines, start=1) if line.strip() == "synthetic)"
        )
        files_line = next(
            index for index, line in enumerate(lines, start=1) if line.strip() == "files)"
        )
        self.assertLess(synthetic_line, line_number)
        self.assertLess(line_number, files_line)
        self.assertEqual(
            command,
            (
                "exec",
                "/usr/sbin/hb-supervisor",
                "--run-synthetic",
                "$instance",
                "$epoch",
                "$role",
                "$test_case",
            ),
        )


if __name__ == "__main__":
    unittest.main()
