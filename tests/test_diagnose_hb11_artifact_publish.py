import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "experiments/vm/diagnose-hb11-artifact-publish.py"
SPEC = importlib.util.spec_from_file_location("diagnose_hb11_artifact_publish", SCRIPT)
assert SPEC and SPEC.loader
DIAGNOSE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSE)


def row(step: str, ret: int = 0, errno: int = 0, skipped: bool = False) -> dict:
    return {"step": step, "return": ret, "errno": errno, "skipped": skipped, "source": None, "target": None}


class ArtifactPublishDiagnosticTests(unittest.TestCase):
    def test_helper_build_command_is_static_aarch64_musl(self) -> None:
        command = DIAGNOSE.helper_build_command(Path("zig"), Path("probe.c"), Path("probe"))
        self.assertEqual(command[:6], ["zig", "cc", "-target", "aarch64-linux-musl", "-static", "-O2"])

    def test_qemu_command_uses_hvf_clone_disks_and_no_network(self) -> None:
        command = DIAGNOSE.build_qemu_command(Path("qemu"), Path("/tmp/clone"), Path("/tmp/s"))
        self.assertIn("hvf", command)
        self.assertIn("none", command[command.index("-nic") + 1:command.index("-nic") + 2])
        self.assertIn("if=virtio,format=raw,file=/tmp/clone/root.raw,readonly=on", command)
        self.assertIn("if=virtio,format=raw,file=/tmp/clone/data.raw,snapshot=off", command)
        self.assertNotIn(str(DIAGNOSE.DEFAULT_IMAGE / "root.raw"), command)

    def test_parse_and_first_failure_identify_linkat_raw_errno(self) -> None:
        records = [row("sync_all"), row("linkat_empty_path", -1, 2), row("post_link_inode_identity", -1, 125, True), row("source_unlink", -1, 125, True)]
        parsed = DIAGNOSE.parse_jsonl("\n".join(json.dumps(item) for item in records))
        self.assertEqual(DIAGNOSE.first_failure(parsed), {"step": "linkat_empty_path", "errno": 2})

    def test_parser_rejects_missing_or_reordered_steps(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed steps"):
            DIAGNOSE.parse_jsonl(json.dumps(row("linkat_empty_path")))

    def test_compare_requires_repeatable_signature_and_unique_first_failure(self) -> None:
        run = [row("sync_all"), row("linkat_empty_path", -1, 2), row("post_link_inode_identity", -1, 125, True), row("source_unlink", -1, 125, True)]
        comparison = DIAGNOSE.compare_runs([run, [dict(item) for item in run]])
        self.assertTrue(comparison["repeatable"])
        self.assertTrue(comparison["uniqueFirstFailure"])
        all_success = [row(step) for step in DIAGNOSE.STEPS]
        self.assertFalse(DIAGNOSE.compare_runs([all_success, all_success])["uniqueFirstFailure"])

    def test_defaults_are_candidate20_clone_only_and_preserve_retained_snapshot(self) -> None:
        args = DIAGNOSE.parser().parse_args(["capture"])
        self.assertEqual(args.image, DIAGNOSE.DEFAULT_IMAGE)
        self.assertEqual(args.data_snapshot, DIAGNOSE.DEFAULT_SNAPSHOT)
        self.assertEqual(args.repeat, 2)
        self.assertNotIn("final", str(args.image).lower())
        source = SCRIPT.read_text()
        self.assertIn('str(args.data_snapshot), str(clone / "data.raw")', source)
        self.assertIn('data_fsck = subprocess.run', source)

    def test_safety_guard_rejects_final_or_other_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.raw"; snapshot.write_bytes(b"x")
            with self.assertRaisesRegex(RuntimeError, "refuses final"):
                DIAGNOSE.validate_sources(Path(directory) / "hb11-final", snapshot)
            with self.assertRaisesRegex(RuntimeError, "candidate-20"):
                DIAGNOSE.validate_sources(Path(directory) / "hb11-candidate-19", snapshot)

    def test_helper_uses_linkat_empty_path_not_shell_link(self) -> None:
        self.assertIn('linkat(fd, "", directory', DIAGNOSE.HELPER_SOURCE)
        self.assertIn("AT_EMPTY_PATH", DIAGNOSE.HELPER_SOURCE)
        self.assertNotIn("system(", DIAGNOSE.HELPER_SOURCE)


if __name__ == "__main__":
    unittest.main()
