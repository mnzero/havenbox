"""Regression checks for the preflight-only acceptance boundary."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROBE = Path(__file__).with_name("run.sh")


class PreflightBoundaryTests(unittest.TestCase):
    def test_invalid_model_with_complete_declarations_cannot_pass_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "llama-server"
            runtime.write_text("#!/bin/sh\nprintf 'test-runtime-version\\n'\n")
            runtime.chmod(0o755)
            # Magic alone passes the existing identity check, but this is no model.
            model = root / "invalid.gguf"
            model.write_bytes(b"GGUF")
            model.with_suffix(".gguf.json").write_text(json.dumps({
                "modelId": "invalid-test-model",
                "quantization": "Q4_K_M",
                "template": "test-template",
                "license": "Apache-2.0",
                "sourceUrlOrCommit": "test-fixture",
                "runtime": "llama-server",
            }))
            schema = root / "tools.json"
            schema.write_text(json.dumps({
                "protocol": "local-http",
                "entryPoints": ["mcp"],
                "inputSchema": {"type": "object"},
                "tools": [{"name": "test", "inputSchema": {"type": "object"}}],
            }))
            completed = subprocess.run([
                str(PROBE), "--runtime", "llama-server", "--model", str(model),
                "--protocol", "local-http", "--tool-entry-point", "mcp",
                "--tool-schema", str(schema), "--offline",
            ], env={**os.environ, "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                    "HB07_NETWORK_DISABLED": "1"}, capture_output=True, text=True, check=False)
            record = json.loads(completed.stdout)
            self.assertEqual(completed.returncode, 2)
            self.assertEqual(record["exitCode"], 2)
            self.assertEqual(record["scope"], "preflight-only")
            self.assertEqual(record["runtimeValidation"], "unverified")
            self.assertEqual(record["preflightResult"], "pass")
            self.assertEqual(record["result"], "unknown")
            for check in ("modelLoading", "gpuExecution", "offline", "toolCalling", "combination"):
                self.assertEqual(record["actual"][check]["status"], "unknown", check)

    def test_non_object_model_metadata_returns_structured_failure(self):
        for value in ([], ["item"], None, "metadata", 0, 1.5, True, False):
            with self.subTest(metadata=value), tempfile.TemporaryDirectory() as directory:
                model = Path(directory) / "invalid.gguf"
                model.write_bytes(b"GGUF")
                metadata = model.with_suffix(".gguf.json")
                metadata.write_text(json.dumps(value))
                completed = subprocess.run(
                    [str(PROBE), "--model", str(model)],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertEqual(completed.stderr, "")
                record = json.loads(completed.stdout)
                self.assertEqual(record["exitCode"], 2)
                self.assertEqual(record["result"], "fail")
                self.assertEqual(record["preflightResult"], "fail")
                self.assertEqual(record["scope"], "preflight-only")
                self.assertEqual(record["runtimeValidation"], "unverified")
                self.assertEqual(record["actual"]["metadata"], {
                    "status": "fail",
                    "reason": "metadata-must-be-object",
                    "path": str(metadata),
                })
                for check in ("modelLoading", "gpuExecution", "offline", "toolCalling", "combination"):
                    self.assertEqual(record["actual"][check]["status"], "unknown", check)

    def test_default_exits_two(self):
        completed = subprocess.run([str(PROBE)], capture_output=True, text=True, check=False)
        record = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(record["exitCode"], 2)
        self.assertEqual(record["result"], "blocked")

    def test_unused_source_is_rejected(self):
        completed = subprocess.run([str(PROBE), "--source", "unused"], capture_output=True, text=True, check=False)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unrecognized arguments: --source", completed.stderr)


if __name__ == "__main__":
    unittest.main()
