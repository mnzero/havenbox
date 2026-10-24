#!/usr/bin/env python3
"""Offline-safe local model candidate probe.

The probe never downloads, installs, or falls back to a network provider. It
reports missing configuration as blocked/unknown and emits JSON suitable for
an evidence record.
"""

import argparse  # noqa: I001
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


RESULTS = {"pass", "fail", "blocked", "unknown"}
RUNTIMES = {
    "llama-server": ["--version"],
    "llama-cli": ["--version"],
    "ollama": ["version"],
    "llamafile": ["--version"],
}


def run(argv: list[str], timeout: float = 5.0) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "exitCode": None, "stdout": "", "stderr": str(exc)}
    return {
        "argv": argv,
        "exitCode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def version(binary: str) -> dict[str, Any]:
    path = shutil.which(binary)
    if not path:
        return {"binary": binary, "status": "blocked", "reason": "runtime-not-found"}
    result = run([path, *RUNTIMES[binary]])
    result.update({"binary": binary, "path": path})
    result["status"] = "pass" if result["exitCode"] == 0 else "fail"
    return result


def file_identity(model: Path | None) -> dict[str, Any]:
    if model is None:
        return {"status": "blocked", "reason": "MODEL_PATH-not-set"}
    if not model.exists():
        return {"status": "blocked", "reason": "model-not-found", "path": str(model)}
    if not model.is_file():
        return {"status": "blocked", "reason": "model-path-is-not-file", "path": str(model)}
    if model.suffix.lower() == ".gguf":
        with model.open("rb") as stream:
            magic = stream.read(4)
        if magic != b"GGUF":
            return {"status": "fail", "reason": "invalid-gguf-magic", "path": str(model), "sha256": sha256(model)}
    return {
        "status": "pass",
        "path": str(model),
        "bytes": model.stat().st_size,
        "sha256": sha256(model),
    }


def resource_snapshot() -> dict[str, Any]:
    result: dict[str, Any] = {"platform": platform.platform(), "machine": platform.machine()}
    if hasattr(os, "sysconf"):
        try:
            result["memoryBytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            result["memoryBytes"] = None
    result["gpuProbe"] = {
        "systemProfiler": run(["system_profiler", "SPDisplaysDataType"], 3)
        if shutil.which("system_profiler")
        else {"status": "unknown", "reason": "system_profiler-not-found"}
    }
    return result


def discover_model_metadata(model: Path | None) -> dict[str, Any]:
    """Read adjacent metadata only; never infer an exact ID from a filename."""
    if model is None:
        return {"status": "unknown", "reason": "no-model"}
    metadata = model.with_suffix(model.suffix + ".json")
    if not metadata.is_file():
        return {"status": "unknown", "reason": "no-adjacent-metadata", "path": str(metadata)}
    try:
        data = json.loads(metadata.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "fail", "reason": "metadata-invalid", "error": str(exc)}
    if not isinstance(data, dict):
        return {"status": "fail", "reason": "metadata-must-be-object", "path": str(metadata)}
    allowed = {"modelId", "quantization", "template", "license", "sourceUrlOrCommit", "runtime"}
    return {"status": "pass", "path": str(metadata), "metadata": {k: data.get(k) for k in allowed}}


def load_tool_schema(path: Path | None, protocol: str | None, entry_point: str | None) -> dict[str, Any]:
    if path is None:
        return {"status": "unknown", "reason": "tool-schema-not-configured"}
    if not path.is_file():
        return {"status": "blocked", "reason": "tool-schema-not-found", "path": str(path)}
    try:
        schema = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "fail", "reason": "tool-schema-invalid-json", "path": str(path), "error": str(exc)}
    if not isinstance(schema, dict):
        return {"status": "fail", "reason": "tool-schema-must-be-object", "path": str(path)}
    errors: list[str] = []
    protocol_value = schema.get("protocol")
    if not isinstance(protocol_value, str) or not protocol_value.strip():
        errors.append("protocol-must-be-non-empty-string")
    entry_points = schema.get("entryPoints")
    if not isinstance(entry_points, list) or not entry_points or not all(
        isinstance(item, str) and item.strip() for item in entry_points
    ):
        errors.append("entryPoints-must-be-non-empty-string-array")
    input_schema = schema.get("inputSchema")
    if not isinstance(input_schema, dict) or not input_schema:
        errors.append("inputSchema-must-be-non-empty-object")
    tools = schema.get("tools")
    if not isinstance(tools, list) or not tools:
        errors.append("tools-must-be-non-empty-array")
    else:
        for index, tool in enumerate(tools):
            if (
                not isinstance(tool, dict)
                or not isinstance(tool.get("name"), str)
                or not tool["name"].strip()
                or not isinstance(tool.get("inputSchema"), dict)
                or not tool["inputSchema"]
            ):
                errors.append(f"tools[{index}]-requires-non-empty-name-and-inputSchema")
    if entry_point and isinstance(entry_points, list) and entry_point not in entry_points:
        errors.append("tool-entry-point-not-declared")
    if protocol and protocol_value != protocol:
        errors.append("tool-protocol-mismatch")
    if errors:
        return {"status": "fail", "reason": "tool-schema-inconsistent", "path": str(path), "errors": errors}
    return {"status": "pass", "path": str(path), "toolCount": len(tools)}


def combination_check(values: dict[str, Any], runtime_name: str, protocol: str | None, entry_point: str | None, tool_schema: dict[str, Any]) -> dict[str, Any]:
    missing = [key for key, value in (("modelId", values.get("modelId")), ("runtime", runtime_name), ("metadata.runtime", values.get("runtime")) ) if not value]
    if missing:
        return {"status": "blocked" if "runtime" in missing or "modelId" in missing else "unknown", "reason": "combination-missing-fields", "missing": missing}
    mismatches: list[str] = []
    if values.get("runtime") != runtime_name:
        mismatches.append("runtime-vs-model-metadata")
    if protocol is None:
        mismatches.append("modelProtocol-not-declared")
    if entry_point is None:
        mismatches.append("toolEntryPoint-not-declared")
    if tool_schema.get("status") != "pass":
        mismatches.append("tool-schema-not-validated")
    if mismatches:
        return {"status": "blocked", "reason": "combination-fields-inconsistent", "mismatches": mismatches}
    return {"status": "pass", "modelId": values["modelId"], "runtime": runtime_name, "modelProtocol": protocol, "toolEntryPoint": entry_point}



def probe(args: argparse.Namespace) -> dict[str, Any]:
    model = Path(args.model).expanduser() if args.model else None
    runtime_name = args.runtime or ""
    runtime = version(runtime_name) if runtime_name in RUNTIMES else {
        "status": "blocked", "reason": "RUNTIME-not-set-or-unsupported", "runtime": runtime_name
    }
    identity = file_identity(model)
    metadata = discover_model_metadata(model)
    values: dict[str, Any] = metadata.get("metadata", {}) if metadata.get("status") == "pass" else {}
    if metadata.get("status") == "pass":
        missing = [key for key in ("modelId", "quantization", "template", "license", "sourceUrlOrCommit", "runtime") if not values.get(key)]
        if missing:
            metadata = {"status": "unknown", "reason": "metadata-missing-required-fields", "missing": missing, "path": metadata["path"]}
            values = {}
    license_value = args.license or values.get("license")
    license_check = {"status": "pass", "value": license_value} if license_value else {"status": "blocked", "reason": "LICENSE-not-declared"}
    offline_isolated = args.offline and os.environ.get("HB07_NETWORK_DISABLED") == "1"
    offline_declaration = {
        "status": "pass" if offline_isolated else "blocked",
        "network": "isolation-declared-by-harness" if offline_isolated else "not-declared",
        "reason": "External declaration only; isolation is not verified by this probe."
        if offline_isolated else "requires external network isolation and HB07_NETWORK_DISABLED=1",
    }
    template_value = values.get("template")
    template_check = {"status": "pass", "value": template_value} if template_value else {"status": "unknown", "reason": "template-not-present-in-model-metadata"}
    tool_check = load_tool_schema(Path(args.tool_schema) if args.tool_schema else None, args.protocol, args.tool_entry_point)
    combination = combination_check(values, runtime_name, args.protocol, args.tool_entry_point, tool_check)
    checks = {
        "runtime": runtime,
        "model": identity,
        "metadata": metadata,
        "license": license_check,
        "offlineDeclaration": offline_declaration,
        "toolSchema": tool_check,
        "template": template_check,
        "preflightCombination": combination,
        "resources": resource_snapshot(),
    }
    if args.min_memory_gib:
        observed = checks["resources"].get("memoryBytes")
        checks["resources"]["minimumGiB"] = args.min_memory_gib
        checks["resources"]["status"] = "pass" if observed and observed >= args.min_memory_gib * 2**30 else "fail"
    statuses = [v["status"] for v in checks.values() if isinstance(v, dict) and v.get("status") in RESULTS]
    preflight_result = "fail" if "fail" in statuses else "blocked" if "blocked" in statuses else "unknown" if "unknown" in statuses else "pass"
    for check in ("modelLoading", "gpuExecution", "offline", "toolCalling", "combination"):
        checks[check] = {"status": "unknown", "reason": "runtime-validation-not-executed"}
    result = preflight_result if preflight_result in {"fail", "blocked"} else "unknown"
    return {
        "schemaVersion": "hb07.local-model-probe.v1",
        "scope": "preflight-only",
        "runtimeValidation": "unverified",
        "preflightResult": preflight_result,
        "candidateId": args.candidate_id,
        "measuredVersion": runtime.get("stdout", "").strip() or None,
        "adapterVersion": "hb07-probe-v1",
        "modelId": values.get("modelId"),
        "modelProtocol": args.protocol,
        "toolEntryPoint": args.tool_entry_point,
        "sourceUrlOrCommit": values.get("sourceUrlOrCommit"),
        "sha256": identity.get("sha256"),
        "license": license_value,
        "command": " ".join([sys.executable, *sys.argv]),
        "environment": {"cwd": os.getcwd(), "python": sys.version, "platform": platform.platform()},
        "expected": "exact local model identity, offline execution, supported schema, and sufficient resources",
        "actual": checks,
        "observations": "No network access, download, installation, or cloud fallback is performed by this probe.",
        "exitCode": 0 if result == "pass" else 2,
        "result": result,
        "evidencePath": args.output or "stdout",
        "recordedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "unverifiedBoundaries": ["model loading", "generation quality", "GPU execution", "offline execution", "tool execution", "runtime combination"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-id", default="local-model-candidate")
    parser.add_argument("--runtime", help="one of llama-server, llama-cli, ollama, llamafile")
    parser.add_argument("--model", help="existing local model file; never downloaded")
    parser.add_argument("--license")
    parser.add_argument("--protocol")
    parser.add_argument("--tool-entry-point")
    parser.add_argument("--tool-schema")
    parser.add_argument("--min-memory-gib", type=float)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    record = probe(args)
    encoded = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(encoded)
    else:
        print(encoded, end="")
    return record["exitCode"]


if __name__ == "__main__":
    raise SystemExit(main())
