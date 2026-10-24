"""HB-08 offline contract checks; NOT a gateway, sandbox or grant issuer.

Only trusted host callers may supply evidence/issued-grant registries and build
flavor. JSON from a guest must never become one of those trusted inputs.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATHS = (
    'Package.swift', 'config/build-toolchain.json', 'scripts/engineering.py',
    'Sources', 'guest/Cargo.toml', 'guest/Cargo.lock',
    'guest/protocol/Cargo.toml', 'guest/adapters/Cargo.toml', 'guest/supervisor/Cargo.toml',
    'guest/protocol/src', 'guest/supervisor/src', 'guest/adapters/src', 'tests/HavenboxCoreTests',
    'tests/generate-header-vectors.py', 'contracts/header-vectors.tsv',
)


def source_snapshot():
    """Hash the candidate engineering inputs, excluding generated/self-referential files."""
    paths = []
    for entry in SNAPSHOT_PATHS:
        path = ROOT / entry
        candidates = sorted(path.rglob('*')) if path.is_dir() else [path]
        paths.extend(p for p in candidates if p.is_file())
    paths = sorted(set(paths))
    relative = [str(path.relative_to(ROOT)) for path in paths]
    payload = b''.join(
        relative_path.encode() + b'\0' + path.read_bytes() + b'\0'
        for relative_path, path in zip(relative, paths, strict=True))
    return relative, hashlib.sha256(payload).hexdigest()


class ContractError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ContractError(message)


def load(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ContractError(f"non-finite JSON number: {value}")

    try:
        return json.loads(Path(path).read_text(), object_pairs_hook=pairs,
                          parse_constant=invalid)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read contract JSON: {path}") from error


def digest(value):
    # hb-canonical-json-v1: UTF-8, sorted keys, no whitespace, no NaN.
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def validate(value, schema, path="$", root=None):
    """Deliberately small JSON Schema subset; unknown keywords fail closed.

    Used only with repository-owned schemas. Not a general JSON Schema engine.
    """
    root = schema if root is None else root
    supported = {"$schema", "$id", "$defs", "$ref", "title", "description", "type",
                 "properties", "required", "additionalProperties", "items", "minItems",
                 "uniqueItems", "minLength", "pattern", "minimum", "maximum", "enum",
                 "const", "oneOf"}
    require(not (set(schema) - supported), f"{path}: unsupported schema keyword")
    if "$ref" in schema:
        ref = schema["$ref"]
        require(ref.startswith("#/$defs/"), "external schema reference forbidden")
        return validate(value, root["$defs"][ref[8:]], path, root)
    if "oneOf" in schema:
        matches = 0
        for choice in schema["oneOf"]:
            try:
                validate(value, choice, path, root)
                matches += 1
            except ContractError:
                pass
        require(matches == 1, f"{path}: expected exactly one variant")
    types = {"object": type(value) is dict, "array": type(value) is list,
             "string": type(value) is str, "integer": type(value) is int,
             "number": type(value) in (int, float) and math.isfinite(value),
             "boolean": type(value) is bool, "null": value is None}
    if "type" in schema:
        expected = schema["type"]
        expected = [expected] if isinstance(expected, str) else expected
        require(any(types.get(t, False) for t in expected), f"{path}: wrong type")
    if "const" in schema:
        require(type(value) is type(schema["const"]) and value == schema["const"],
                f"{path}: wrong constant")
    if "enum" in schema:
        require(any(type(value) is type(v) and value == v for v in schema["enum"]),
                f"{path}: unknown enum")
    if isinstance(value, dict):
        require(set(schema.get("required", [])) <= value.keys(), f"{path}: missing field")
        props = schema.get("properties", {})
        extra = value.keys() - props.keys()
        require(schema.get("additionalProperties", True) is not False or not extra,
                f"{path}: unknown field {sorted(extra)}")
        for key, item in value.items():
            if key in props:
                validate(item, props[key], f"{path}.{key}", root)
    if isinstance(value, list):
        require(len(value) >= schema.get("minItems", 0), f"{path}: too few items")
        if schema.get("uniqueItems"):
            require(len({digest(v) for v in value}) == len(value), f"{path}: duplicates")
        for index, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{path}[{index}]", root)
    if isinstance(value, str):
        require(len(value) >= schema.get("minLength", 0), f"{path}: empty string")
        if "pattern" in schema:
            require(re.search(schema["pattern"], value) is not None, f"{path}: pattern")
    if type(value) in (int, float):
        require(math.isfinite(value), f"{path}: non-finite number")
        require(value >= schema.get("minimum", -math.inf), f"{path}: below minimum")
        require(value <= schema.get("maximum", math.inf), f"{path}: above maximum")


def check_schema(value, name):
    validate(value, load(ROOT / "config/schemas" / f"{name}.schema.json"))
    if name == "inference-parameter":
        # JSON Schema cannot compare sibling values; check after shape/types,
        # including defaults even when a caller supplies an override.
        for key in ("contextTokens", "maxOutputTokens"):
            rule = value["fields"][key]
            bounds = rule["valueSchema"]
            path = f"$.fields.{key}"
            require(bounds["minimum"] <= bounds["maximum"],
                    f"{path}: minimum exceeds maximum")
            validate(rule["default"], bounds, f"{path}.default")


def binding(manifest, combination):
    # Includes every dependency, policy, asset and protocol/schema content digest.
    # State/evidence excluded from combination to avoid self-referential receipts.
    return digest({"manifest": manifest, "combination": {
        key: value for key, value in combination.items() if key not in ("state", "evidence")}})


def check_engineering_ci():
    """Guard only the current engineering workflow's literal checkout step.

    This is deliberately not a YAML interpreter: changes to this step's shape
    require policy review. The action implementation is pinned; repository input
    must remain the event commit, never that action SHA or an old source commit.
    """
    lines = [line.split('#', 1)[0].rstrip() for line in
             (ROOT / '.github/workflows/engineering.yml').read_text().splitlines()]
    lines = [line for line in lines if line.strip()]
    starts = [i for i, line in enumerate(lines) if 'actions/checkout' in line]
    require(len(starts) == 1, 'CI checkout policy: expected one literal checkout step')
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith('      - ')), len(lines))
    require(lines[start:end] == [
        '      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683',
        '        with:',
        '          ref: ${{ github.sha }}',
        '          persist-credentials: false',
    ], 'CI checkout policy: pin action SHA but checkout repository at github.sha')


def check_inventory(manifest, matrix):
    check_engineering_ci()
    check_schema(manifest, "runtime-manifest")
    check_schema(matrix, "compatibility-matrix")
    snapshot_paths, snapshot_digest = source_snapshot()
    require(manifest["sourceSnapshot"]["baseCommit"] == manifest["sourceCommit"],
            "source snapshot base commit differs from sourceCommit")
    require(manifest["sourceSnapshot"]["paths"] == snapshot_paths,
            "source snapshot path list drift")
    require(manifest["sourceSnapshot"]["sha256"] == snapshot_digest,
            "source snapshot content drift")
    for field in ("dependencies", "contracts"):
        ids = [item["id"] for item in manifest[field]]
        require(len(ids) == len(set(ids)), f"duplicate {field} id")
    required_paths = {str(p.relative_to(ROOT)) for p in (ROOT / "config/schemas").glob("*.json")}
    required_paths.update({"config/inference-parameters.json", "contracts/baseline.py",
                           "contracts/protocol.md", "tests/contracts/test_baseline.py",
                           "scripts/check-baseline.py",
                           "config/build-toolchain.json", "contracts/header-vectors.tsv",
                           "Package.swift", "guest/Cargo.lock", "scripts/engineering.py",
                           "Sources/HavenboxCore/ProtocolHeader.swift", "Sources/HavenboxCore/WireID.swift",
                           "guest/protocol/src/lib.rs", "tests/HavenboxCoreTests/ProtocolTests.swift",
                           "tests/generate-header-vectors.py"})
    require({item["path"] for item in manifest["contracts"]} == required_paths,
            "incomplete contract inventory")
    required_dependencies = {
        "swift-toolchain", "rust-toolchain", "qemu", "qemu-img", "linux-kernel", "guest-image",
        "node-runtime", "python-runtime", "kimi-cli", "pi", "dsh", "builtin-adapter",
        "kimi-adapter", "pi-adapter", "dsh-adapter", "harness-patches", "llama-runtime",
        "local-model", "model-template", "mcp-protocol", "mcp-sdk", "mcp-server", "csv-engine",
        "cli", "sqlite", "watchdog", "supervisor", "gateway", "check-suite"}
    require({d["id"] for d in manifest["dependencies"]} == required_dependencies,
            "incomplete build dependency inventory")
    ids = [item["id"] for item in matrix["combinations"]]
    require(len(ids) == len(set(ids)), "duplicate combination id")
    for artifact in manifest["contracts"]:
        path = ROOT / artifact["path"]
        require(path.resolve().is_relative_to(ROOT), "contract outside repository")
        require(path.is_file(), f"missing contract: {artifact['path']}")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"],
                f"contract drift: {artifact['path']}")


def select(matrix, combination_id):
    found = [c for c in matrix["combinations"] if c["id"] == combination_id]
    require(len(found) == 1, "unknown or duplicate combination")
    return found[0]


def authorize_ordinary(manifest, matrix, combination_id, trusted_evidence):
    """Checks qualification only, never grants execution/disclosure permission."""
    check_inventory(manifest, matrix)
    combination = select(matrix, combination_id)
    require(manifest["state"] == "frozen", "baseline not frozen")
    require(combination["state"] in ("validated", "frozen"), "unverified combination")
    require(not manifest["unresolved"], "unresolved baseline")
    for dep in manifest["dependencies"]:
        require(all(dep[k] is not None for k in ("version", "sha256", "license", "sbom")),
                f"unresolved dependency: {dep['id']}")
    require(manifest["platform"]["minimumOSVerified"], "minimum OS unverified")
    require(manifest["platform"]["referenceHardware"] is not None, "reference device unverified")
    require(manifest["sqlite"]["version"] is not None, "SQLite fix build unresolved")
    sqlite = next(d for d in manifest["dependencies"] if d["id"] == "sqlite")
    require(sqlite["version"] == manifest["sqlite"]["version"], "SQLite version mismatch")
    check_route(combination)
    expected = binding(manifest, combination)
    evidence = combination["evidence"]
    for gate in ("functional", "security", "release"):
        receipt = evidence[gate]
        require(receipt is not None, f"missing {gate} evidence")
        require(receipt in trusted_evidence, "untrusted evidence")
        record = trusted_evidence[receipt]
        require(record == {"binding": expected, "gate": gate, "result": "pass"},
                f"stale or failed {gate} evidence")
    return expected


def check_route(combination):
    require(combination["harness"] != "pending" and combination["adapterRevision"] is not None,
            "unresolved harness/adapter")
    route = combination["route"]
    common = ("protocol", "modelId")
    required = (("providerRevision", "credentialVersion", "endpointRevision")
                if route["mode"] == "online" else
                ("resourceRevision", "runtimeRevision", "templateDigest", "quantization"))
    forbidden = (("resourceRevision", "runtimeRevision", "templateDigest", "quantization")
                 if route["mode"] == "online" else
                 ("providerRevision", "credentialVersion", "endpointRevision"))
    require(all(route[key] is not None for key in common + required), "unresolved route")
    require(all(route[key] is None for key in forbidden), "mixed route")


def authorize_validation(manifest, matrix, grant, request, *, build_flavor,
                         issued_grants, synthetic_datasets, now):
    """Pure preflight. Caller must atomically reserve cumulative budget before use.

    issued_grants maps unguessable IDs to exact host-approved records. A digest
    alone is not authenticity. This function does not mint/consume a capability.
    """
    require(build_flavor == "internal-validation", "validation disabled in this build")
    check_inventory(manifest, matrix)
    check_schema(grant, "validation-run-grant")
    check_schema(request, "validation-request")
    require(grant["id"] in issued_grants and issued_grants[grant["id"]] == grant,
            "grant not issued or revoked")
    combination = select(matrix, grant["combinationId"])
    check_route(combination)
    require(all(dep["version"] is not None and dep["sha256"] is not None
                for dep in manifest["dependencies"]), "candidate assets not pinned")
    require(grant["binding"] == binding(manifest, combination), "candidate drift")
    require(grant["notBefore"] <= now < grant["expiresAt"], "grant not active")
    require(0 < grant["expiresAt"] - grant["notBefore"] <= 3600, "grant TTL exceeds policy")
    require(grant["datasetDigest"] in synthetic_datasets, "unapproved synthetic dataset")
    for key in ("combinationId", "datasetDigest", "routeDigest", "epoch", "sessionId", "turnId"):
        require(request[key] == grant[key], f"grant scope mismatch: {key}")
    require(grant["routeDigest"] == digest(combination["route"]), "route drift")
    require(request["authorizationDigest"] == grant["authorizationDigest"], "authorization mismatch")
    require(type(now) is int and now >= 0, "invalid trusted clock")
    caps = dict(zip(("requests", "inputTokens", "outputTokens", "bytes", "toolCalls", "seconds"),
                    (12, 98304, 24576, 50331648, 20, 600), strict=True))
    for key, cap in caps.items():
        require(grant["limits"][key] <= cap, f"grant exceeds validation policy: {key}")
    for key, amount in request["reservation"].items():
        require(amount <= grant["limits"][key], f"grant limit exceeded: {key}")
    return "qualification-only; all product boundary checks still required"


def normalize_parameters(schema, supplied, *, input_tokens, template_tokens, tool_tokens):
    check_schema(schema, "inference-parameter")
    require(isinstance(supplied, dict), "parameters must be an object")
    fields = schema["fields"]
    require(not (supplied.keys() - fields.keys()), "unknown parameter")
    result = {}
    for key, rule in fields.items():
        if not rule["supported"]:
            require(key not in supplied, f"unsupported parameter: {key}")
            continue
        value = supplied.get(key, rule["default"])
        validate(value, rule["valueSchema"], key)
        result[key] = value
    for pair in schema["mutuallyExclusive"]:
        require(sum(key in supplied for key in pair) <= 1, "mutually exclusive parameters")
    counts = (input_tokens, template_tokens, tool_tokens)
    require(all(type(n) is int and n >= 0 for n in counts), "invalid token accounting")
    require(sum(counts) + result["maxOutputTokens"] <= result["contextTokens"],
            "context budget exceeded; explicit summary or configuration required")
    return {"effective": result, "schemaDigest": digest(schema)}
