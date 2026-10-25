#!/usr/bin/env python3
"""Diagnose candidate-20 artifact publication on disposable real-VM clones."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / "experiments/vm/assets/hb11-candidate-20"
DEFAULT_SNAPSHOT = (
    ROOT / "experiments/vm/state/mvp02-candidate20-gates-retry/"
    "runtime-evidence/stage2-failed-data.raw"
)
DEFAULT_STATE = ROOT / "experiments/vm/state/hb11-candidate20-artifact-publish-premise"
STEPS = ("sync_all", "linkat_empty_path", "post_link_inode_identity", "source_unlink")

HELPER_SOURCE = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

static const char *root = "/data/active";
static const char *source = "/data/active/tmp/mvp02-result.csv";
static const char *target = "/data/active/artifacts/file_mvp02-result.csv";
static const char *result_path = "/data/hb11-artifact-publish/result.jsonl";

static void die(const char *what) { perror(what); _exit(111); }
static void make_dir(const char *path, mode_t mode) {
    if (mkdir(path, mode) != 0 && errno != EEXIST) die(path);
    if (chmod(path, mode) != 0) die("chmod directory");
}
static void copy_mountinfo(void) {
    int in = open("/proc/self/mountinfo", O_RDONLY | O_CLOEXEC);
    int out = open("/data/hb11-artifact-publish/mountinfo", O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (in < 0 || out < 0) die("mountinfo open");
    char buffer[4096]; ssize_t count;
    while ((count = read(in, buffer, sizeof buffer)) > 0) {
        char *cursor = buffer;
        while (count > 0) { ssize_t n = write(out, cursor, (size_t)count); if (n < 0) die("mountinfo write"); cursor += n; count -= n; }
    }
    if (count < 0 || fsync(out) != 0) die("mountinfo copy");
    close(in); close(out);
}
static void stat_json(FILE *out, const char *path) {
    struct stat value;
    if (lstat(path, &value) != 0) { fputs("null", out); return; }
    fprintf(out, "{\"dev\":\"%u:%u\",\"ino\":%" PRIuMAX ",\"nlink\":%" PRIuMAX ",\"mode\":\"%04o\"}",
        major(value.st_dev), minor(value.st_dev), (uintmax_t)value.st_ino,
        (uintmax_t)value.st_nlink, value.st_mode & 07777);
}
static void record(FILE *out, const char *step, int ret, int raw_errno, int skipped) {
    fprintf(out, "{\"step\":\"%s\",\"return\":%d,\"errno\":%d,\"skipped\":%s,\"source\":", step, ret, raw_errno, skipped ? "true" : "false");
    stat_json(out, source); fputs(",\"target\":", out); stat_json(out, target); fputs("}\n", out); fflush(out); fsync(fileno(out));
}
int main(void) {
    umask(0);
    make_dir("/data/hb11-artifact-publish", 0700);
    make_dir(root, 0755); make_dir("/data/active/input", 0755);
    make_dir("/data/active/incoming", 0755); make_dir("/data/active/tmp", 0755);
    make_dir("/data/active/artifacts", 0755);
    copy_mountinfo();
    struct stat staged;
    if (lstat(source, &staged) != 0 || !S_ISREG(staged.st_mode) || staged.st_nlink != 1 || (staged.st_mode & 07777) != 0444) die("staged source precondition");
    if (lstat(target, &staged) == 0 || errno != ENOENT) die("target absence precondition");
    int fd = open(source, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) die("source reopen");
    struct stat initial;
    if (fstat(fd, &initial) != 0 || !S_ISREG(initial.st_mode) || initial.st_nlink != 1 || initial.st_size <= 0) die("source metadata");
    char verify[4096];
    while (read(fd, verify, sizeof verify) > 0) {}
    if (fchmod(fd, 0444) != 0) die("source chmod");
    int directory = open("/data/active/artifacts", O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (directory < 0) die("artifact directory open");
    FILE *out = fopen(result_path, "w"); if (!out) die("result open");
    int failed = 0, ret, saved;
    errno = 0; ret = fsync(fd); saved = errno; record(out, "sync_all", ret, saved, 0); if (ret != 0) failed = 1;
    if (!failed) { errno = 0; ret = linkat(fd, "", directory, "file_mvp02-result.csv", AT_EMPTY_PATH); saved = errno; record(out, "linkat_empty_path", ret, saved, 0); if (ret != 0) failed = 1; }
    else record(out, "linkat_empty_path", -1, ECANCELED, 1);
    if (!failed) {
        struct stat opened, current_source, published; errno = 0;
        ret = fstat(fd, &opened); if (ret == 0) ret = lstat(source, &current_source); if (ret == 0) ret = lstat(target, &published);
        saved = errno;
        if (ret == 0 && (opened.st_dev != current_source.st_dev || opened.st_ino != current_source.st_ino || opened.st_dev != published.st_dev || opened.st_ino != published.st_ino)) { ret = -1; saved = ESTALE; }
        record(out, "post_link_inode_identity", ret, saved, 0); if (ret != 0) failed = 1;
    } else record(out, "post_link_inode_identity", -1, ECANCELED, 1);
    if (!failed) { errno = 0; ret = unlink(source); saved = errno; record(out, "source_unlink", ret, saved, 0); if (ret != 0) failed = 1; }
    else record(out, "source_unlink", -1, ECANCELED, 1);
    fsync(fileno(out)); fclose(out); fsync(directory); close(directory); close(fd);
    int data = open("/data", O_RDONLY | O_DIRECTORY | O_CLOEXEC); if (data >= 0) { fsync(data); close(data); }
    sync();
    dprintf(STDERR_FILENO, "HB11_ARTIFACT_PUBLISH_DIAG complete=true failed=%s\n", failed ? "true" : "false");
    return failed ? 1 : 0;
}
'''


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_checked(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, **kwargs)


def helper_build_command(zig: Path, source: Path, output: Path) -> list[str]:
    return [str(zig), "cc", "-target", "aarch64-linux-musl", "-static", "-O2", "-Wall", "-Wextra", str(source), "-o", str(output)]


def build_qemu_command(qemu: Path, clone: Path, sockets: Path) -> list[str]:
    command = [
        str(qemu), "-M", "virt", "-accel", "hvf", "-cpu", "host", "-smp", "2",
        "-m", "4096M", "-nodefaults", "-nographic", "-nic", "none", "-serial",
        "stdio", "-monitor", "none", "-kernel", str(clone / "Image"), "-initrd",
        str(clone / "initramfs-hb11"), "-append",
        "console=ttyAMA0 root=/dev/vda ro init=/sbin/hb-init "
        "hb.instance=00112233445566778899aabbccddeeff hb.epoch=1 hb.mode=files",
        "-drive", f"if=virtio,format=raw,file={clone / 'root.raw'},readonly=on",
        "-drive", f"if=virtio,format=raw,file={clone / 'data.raw'},snapshot=off",
        "-device", "virtio-serial-pci,id=hbserial",
    ]
    for name in ("control", "file", "inference"):
        command.extend(["-chardev", f"socket,id={name},path={sockets / (name + '.sock')},server=on,wait=off", "-device", f"virtserialport,bus=hbserial.0,chardev={name},name=hb.{name}"])
    return command


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in text.splitlines() if line]
    names = tuple(record.get("step") for record in records)
    if names != STEPS:
        raise ValueError(f"expected fixed steps {STEPS}, got {names}")
    for record in records:
        if not isinstance(record.get("return"), int) or not isinstance(record.get("errno"), int):
            raise ValueError("step return and errno must be integers")
    return records


def first_failure(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    failures = [record for record in records if not record.get("skipped") and record["return"] != 0]
    if not failures:
        return None
    first = failures[0]
    if any(not record.get("skipped") for record in records[records.index(first) + 1:]):
        raise ValueError("a step ran after the first failure")
    return {"step": first["step"], "errno": first["errno"]}


def signature(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"step": row["step"], "return": row["return"], "errno": row["errno"], "skipped": row["skipped"]} for row in records]


def compare_runs(runs: list[list[dict[str, Any]]]) -> dict[str, Any]:
    signatures = [signature(run) for run in runs]
    failures = [first_failure(run) for run in runs]
    return {"repeatable": len(runs) >= 2 and all(item == signatures[0] for item in signatures[1:]), "signatures": signatures, "firstFailures": failures, "uniqueFirstFailure": bool(failures) and failures[0] is not None and len({json.dumps(item, sort_keys=True) for item in failures}) == 1}


def debugfs_dump(debugfs: Path, image: Path, guest_path: str, host_path: Path) -> None:
    run_checked([str(debugfs), "-R", f"dump {guest_path} {host_path}", str(image)], capture_output=True)
    if not host_path.is_file():
        raise RuntimeError(f"debugfs did not extract {guest_path}")


def validate_sources(image: Path, snapshot: Path) -> None:
    if image.resolve() != DEFAULT_IMAGE.resolve() or "final" in image.name.lower():
        raise RuntimeError("clone-only diagnostic is pinned to hb11-candidate-20 and refuses final")
    if not snapshot.is_file() or snapshot.is_symlink():
        raise RuntimeError("retained data snapshot must be a regular non-symlink file")


def run_once(args: argparse.Namespace, helper: Path, staging: Path, number: int) -> tuple[list[dict[str, Any]], list[str]]:
    run_dir = staging / f"run-{number}"
    run_dir.mkdir(parents=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"hb11-publish-{number}-", dir="/tmp"))
    process: subprocess.Popen[str] | None = None
    try:
        clone = temporary / "candidate"; clone.mkdir()
        for name in ("Image", "initramfs-hb11", "root.raw"):
            run_checked(["cp", "-c", str(args.image / name), str(clone / name)])
        run_checked(["cp", "-c", str(args.data_snapshot), str(clone / "data.raw")])
        (clone / "root.raw").chmod(0o600); (clone / "data.raw").chmod(0o600)
        for command in ("unlink /usr/sbin/hb-supervisor", f"write {helper} /usr/sbin/hb-supervisor", "set_inode_field /usr/sbin/hb-supervisor mode 0100755"):
            run_checked([str(args.debugfs), "-w", "-R", command, str(clone / "root.raw")], capture_output=True)
        fsck = subprocess.run([str(args.e2fsck), "-fy", str(clone / "root.raw")], text=True, capture_output=True)
        if fsck.returncode not in (0, 1):
            raise RuntimeError(f"e2fsck failed: {fsck.stderr}")
        data_fsck = subprocess.run([str(args.e2fsck), "-fy", str(clone / "data.raw")], text=True, capture_output=True)
        if data_fsck.returncode not in (0, 1):
            raise RuntimeError(f"data e2fsck failed: {data_fsck.stderr}")
        sockets = temporary / "s"; sockets.mkdir()
        command = build_qemu_command(args.qemu, clone, sockets)
        environment = os.environ.copy(); environment["HB02_QEMU_SYSTEM_FRAMEWORK"] = str(args.framework)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment)
        try:
            serial, _ = process.communicate(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try: serial, _ = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); serial, _ = process.communicate()
        (run_dir / "qemu.log").write_text(serial)
        try:
            debugfs_dump(args.debugfs, clone / "data.raw", "/hb11-artifact-publish/result.jsonl", run_dir / "result.jsonl")
        except RuntimeError as error:
            raise RuntimeError(f"{error}; QEMU tail: {serial[-4000:]}") from error
        debugfs_dump(args.debugfs, clone / "data.raw", "/hb11-artifact-publish/mountinfo", run_dir / "mountinfo")
        records = parse_jsonl((run_dir / "result.jsonl").read_text())
        return records, command
    finally:
        if process is not None and process.poll() is None:
            process.kill(); process.wait()
        shutil.rmtree(temporary)


def capture_command(args: argparse.Namespace) -> int:
    args.image = args.image.resolve(); args.data_snapshot = args.data_snapshot.resolve(); args.state_dir = args.state_dir.resolve()
    validate_sources(args.image, args.data_snapshot)
    manifest = json.loads((args.image / "manifest.json").read_text())
    candidate_paths = [args.image / name for name in ("Image", "initramfs-hb11", "root.raw", "data.raw")]
    before = {path.name: sha256(path) for path in candidate_paths}
    for path in candidate_paths:
        if before[path.name] != manifest["files"][path.name]["sha256"]:
            raise RuntimeError(f"candidate hash mismatch: {path.name}")
    snapshot_before = sha256(args.data_snapshot)
    with tempfile.TemporaryDirectory(prefix="hb11-publish-build-", dir="/tmp") as build_name, tempfile.TemporaryDirectory(prefix="hb11-publish-evidence-", dir="/tmp") as evidence_name:
        build = Path(build_name); source = build / "diagnostic.c"; helper = build / "diagnostic"
        source.write_text(HELPER_SOURCE)
        build_command = helper_build_command(args.zig, source, helper)
        run_checked(build_command, capture_output=True)
        file_output = run_checked(["/usr/bin/file", str(helper)], capture_output=True).stdout.strip()
        if "ARM aarch64" not in file_output or "statically linked" not in file_output:
            raise RuntimeError(f"unexpected helper binary: {file_output}")
        staging = Path(evidence_name) / "evidence"; staging.mkdir()
        runs: list[list[dict[str, Any]]] = []; commands: list[list[str]] = []
        for number in range(1, args.repeat + 1):
            records, command = run_once(args, helper, staging, number); runs.append(records); commands.append(command)
        comparison = compare_runs(runs)
        (staging / "comparison.json").write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n")
        after = {path.name: sha256(path) for path in candidate_paths}; snapshot_after = sha256(args.data_snapshot)
        if after != before or snapshot_after != snapshot_before:
            raise RuntimeError("candidate or retained snapshot changed")
        receipt = {"schemaVersion": "havenbox.hb11-artifact-publish-premise.v1", "candidate": str(args.image.relative_to(ROOT)), "retainedSnapshot": str(args.data_snapshot.relative_to(ROOT)), "candidateHashesBefore": before, "candidateHashesAfter": after, "retainedSnapshotSha256Before": snapshot_before, "retainedSnapshotSha256After": snapshot_after, "helperSha256": sha256(helper), "helperFile": file_output, "helperBuildCommand": [part.replace(str(build), "/tmp/<build>") for part in build_command], "qemuCommands": commands, "comparison": comparison, "temporaryClonesRemoved": True, "qemuVersion": run_checked([str(args.qemu), "--version"], env={**os.environ, "HB02_QEMU_SYSTEM_FRAMEWORK": str(args.framework)}, capture_output=True).stdout.splitlines()[0]}
        (staging / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        if args.state_dir.exists():
            shutil.rmtree(args.state_dir)
        args.state_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, args.state_dir)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if comparison["repeatable"] and comparison["uniqueFirstFailure"] else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("--image", type=Path, default=DEFAULT_IMAGE)
    capture.add_argument("--data-snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    capture.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    capture.add_argument("--repeat", type=int, default=2, choices=range(2, 6))
    capture.add_argument("--timeout", type=float, default=12.0)
    capture.add_argument("--zig", type=Path, default=Path("/opt/homebrew/bin/zig"))
    capture.add_argument("--qemu", type=Path, default=ROOT / "experiments/vm/tools/qemu-system-aarch64")
    capture.add_argument("--framework", type=Path, default=ROOT / "experiments/vm/tools/qemu-aarch64-softmmu")
    capture.add_argument("--debugfs", type=Path, default=ROOT / "experiments/vm/tools/debugfs")
    capture.add_argument("--e2fsck", type=Path, default=ROOT / "experiments/vm/tools/e2fsck")
    capture.set_defaults(function=capture_command)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
