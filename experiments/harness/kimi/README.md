# HB-04 Kimi CLI / ACP experiments

Two deliberately separate entry points:

- `run.sh` / `probe.py`: unchanged metadata security preflight. Default starts no
  subprocess; eight original security regressions remain intact. Its legacy
  `/usr/local/bin/kimi` deployment and unsupported `acp --version` profile are not
  the executable or protocol used by the new experiment.
- `protocol_probe.py --local-fixture`: explicit opt-in to a real Kimi ACP subprocess
  plus a synthetic loopback OpenAI-compatible HTTP fixture. **Not a real model,
  external provider, guest execution, or VM/network isolation acceptance.**

## Run

Python 3.11+ standard library, no pip dependencies. This machine's existing Kimi
Code CLI 2.2.0 is pinned in `acp_client.py` by absolute path and SHA-256. No PATH
lookup, download, global install, automatic upgrade, or credential discovery.
A different deployment requires a reviewed source change and fresh evidence.

```sh
# Existing safe default (no subprocess)
experiments/harness/kimi/run.sh /tmp/hb04-metadata.json
# Unit regressions (real Kimi test explicitly skipped)
python3 -m unittest discover -s experiments/harness/kimi -p 'test_*.py' -v
# Explicit synthetic local integration, no paid / external model
python3 experiments/harness/kimi/protocol_probe.py --local-fixture --output /tmp/hb04-acp.json
HB04_REAL_ACP=1 python3 -m unittest discover -s experiments/harness/kimi -p 'test_*.py' -v
```

## Contract and limits

ACP v1 newline-delimited JSON-RPC is used as specified by ACP and Kimi's official
2.2.0 documentation (links and release commit in `doc/verification/HB-04.md`).
The client implements initialize/version negotiation, new session, request ID
correlation including out-of-order replies, session updates, asynchronous prompt,
cancel notification, remote errors and deterministic process-group teardown.
Only one prompt is active. Client fs/terminal capabilities are absent, permission
requests return `cancelled`, other reverse requests return -32601; no host tool
is executed. This does **not** prove Kimi's own internal tools have no bypass.

Limits: 64 KiB per frame/write, 1 MiB total received (stdout+stderr) and total
sent, 256 events, 8 outstanding requests, default 15-second I/O deadlines.
Raw stderr is counted then discarded; remote error text is not echoed. Protocol
exceptions leave teardown to the context manager; always use `with ACPClient`.

The runner owns an exclusive 0700 temporary HOME, writes managed config mode
0600, sets cwd inside it, and removes it after reaping Kimi. Environment is built
from scratch; user credentials, proxies, loader flags and model overrides are
not inherited. Telemetry, update checks and cron are disabled. Fixed rejecting
loopback proxies are defense in depth, **not an OS network sandbox**; loopback is
exempt for the fixture. No authentication flow is invoked. `managed.toml` routes
only to loopback and exposes no tools via a nonempty unmatched allowlist (empty
`enabled=[]` would NOT disable tools). The synthetic API key is not a credential.

Prompt `wait(id)` returns `stopReason`, `protocol_status="completed"`, and
`business_outcome="unknown"` (or `cancelled` for protocol cancellation). A
completed JSON-RPC prompt is not business success; neither text nor `end_turn`
can promote unknown. Raw remote result fields cannot override this contract.
Remote JSON-RPC errors still raise `RemoteError`. No production success-evidence
channel or provider gateway has been implemented.

Observed defect: fixture HTTP 400 yields ACP `end_turn` with only usage update,
not a visible protocol error. A second failure fixture sends HTTP 200, READY,
then an SSE error with code 400 (HTTP status cannot change after headers);
partial text also ends with `end_turn`. The serial, local-only probe associates
fixture requests by each prompt's request window and reports these as business
`failed` with error propagation `blocked`, without changing client unknown.
The normal fixture remains protocol pass / business unknown.

`managed.toml` uses the documented `[background] keep_alive_on_exit=false`.
The probe verifies the written TOML; the pinned upstream schema/resolver supports
this key. This is not runtime introspection or a background-process teardown test. The report marks provider error visibility blocked;
invalid method/mode JSON-RPC errors are separately tested and observable. The
fixture emits READY deterministically and must never be presented as model proof.
Native recovery, tool refusal against the real harness, real gateway/VM/provider
validation and hidden-context authorization remain unknown/blocked.
