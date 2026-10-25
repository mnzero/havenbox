import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "experiments/vm/diagnose-hb11-boot-contract.py"
SPEC = importlib.util.spec_from_file_location("diagnose_hb11_boot_contract", SCRIPT)
assert SPEC and SPEC.loader
DIAGNOSE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSE)


CAPTURED_MOUNTINFO = """27 1 253:0 / / ro,relatime - ext4 /dev/vda ro
24 27 0:6 / /dev rw,nosuid,noexec,relatime - devtmpfs devtmpfs rw,size=2003720k,nr_inodes=500930,mode=755,inode64
25 24 0:23 / /dev/pts rw,nosuid,noexec,relatime - devpts devpts rw,gid=5,mode=620,ptmxmode=000
26 27 0:24 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
28 27 0:25 / /sys ro,nosuid,nodev,noexec,relatime - sysfs sysfs ro
29 28 0:26 / /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime - cgroup2 cgroup rw
30 27 0:27 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=65536k,inode64
31 27 253:16 / /data rw,nosuid,nodev,noexec,relatime - ext4 /dev/vdb rw,prjquota
"""


class BootContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = DIAGNOSE.read_boot_mounts(ROOT / "guest/supervisor/src/lib.rs")

    def test_captured_inventory_reproduces_only_devmount_difference(self) -> None:
        result = DIAGNOSE.compare_inventory(
            CAPTURED_MOUNTINFO,
            ["lo"],
            [("vda", "253:0"), ("vdb", "253:16")],
            self.expected,
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(
            result["differences"],
            [{
                "kind": "mount-super-options",
                "path": "/dev",
                "expected": "rw,size=65536k,nr_inodes=16384,mode=755,inode64",
                "actual": "rw,size=2003720k,nr_inodes=500930,mode=755,inode64",
            }],
        )

    def test_matching_inventory_is_accepted(self) -> None:
        matching = CAPTURED_MOUNTINFO.replace(
            "size=2003720k,nr_inodes=500930",
            "size=65536k,nr_inodes=16384",
        )
        result = DIAGNOSE.compare_inventory(
            matching,
            ["lo"],
            [("vda", "253:0"), ("vdb", "253:16")],
            self.expected,
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["differences"], [])

    def test_reports_every_difference_instead_of_first_boundary(self) -> None:
        drifted = CAPTURED_MOUNTINFO.replace(
            "devtmpfs rw,size=2003720k,nr_inodes=500930,mode=755,inode64",
            "devtmpfs rw,size=65536k,nr_inodes=16284,mode=755",
        ).replace(
            "tmpfs tmpfs rw,size=65536k,inode64",
            "tmpfs tmpfs rw,size=65536k",
        )
        result = DIAGNOSE.compare_inventory(
            drifted,
            ["eth0", "lo"],
            [("vda", "253:0"), ("vdb", "253:16"), ("vdc", "253:32")],
            self.expected,
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(
            [(difference["kind"], difference.get("path")) for difference in result["differences"]],
            [
                ("network-inventory", None),
                ("block-inventory", None),
                ("mount-super-options", "/dev"),
                ("mount-super-options", "/tmp"),
            ],
        )

    def test_parse_inventory_preserves_captured_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory)
            (inventory / "mountinfo").write_text(CAPTURED_MOUNTINFO)
            (inventory / "network.txt").write_text("lo\n")
            (inventory / "blocks.txt").write_text("vda 253:0\nvdb 253:16\n")
            mountinfo, network, blocks = DIAGNOSE.parse_inventory(inventory)
        self.assertEqual(mountinfo, CAPTURED_MOUNTINFO)
        self.assertEqual(network, ["lo"])
        self.assertEqual(blocks, [("vda", "253:0"), ("vdb", "253:16")])

    def test_capture_remount_flag_generates_explicit_command_and_diagnostics(self) -> None:
        script = DIAGNOSE.wrapper_script(devtmpfs_remount=True)
        self.assertIn(
            "mount -o remount,nosuid,noexec,size=65536k,nr_inodes=16384,mode=755 "
            "devtmpfs /dev 2>\"$directory/remount.stderr\"",
            script,
        )
        self.assertIn("printf '%s\\n' \"$remount_status\" >\"$directory/remount.status\"", script)
        self.assertLess(script.index("mount -o remount"), script.index("cat /proc/self/mountinfo"))

    def test_capture_defaults_to_no_devmount_remount(self) -> None:
        args = DIAGNOSE.parser().parse_args([
            "capture",
            "--image", "candidate",
            "--data-snapshot", "snapshot.raw",
            "--state-dir", "state",
        ])
        self.assertFalse(args.devtmpfs_remount)
        self.assertNotIn("mount -o remount", DIAGNOSE.wrapper_script())

    def test_capture_parses_devmount_remount_switch(self) -> None:
        args = DIAGNOSE.parser().parse_args([
            "capture",
            "--image", "candidate",
            "--data-snapshot", "snapshot.raw",
            "--state-dir", "state",
            "--devtmpfs-remount",
        ])
        self.assertTrue(args.devtmpfs_remount)

    def test_failed_remount_is_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inventory = Path(directory)
            (inventory / "remount.status").write_text("32\n")
            (inventory / "remount.stderr").write_text("mount: permission denied\n")
            diagnostic = DIAGNOSE.read_remount_diagnostic(inventory)
        self.assertEqual(
            diagnostic,
            {
                "enabled": True,
                "status": 32,
                "stderr": "mount: permission denied\n",
            },
        )


if __name__ == "__main__":
    unittest.main()
