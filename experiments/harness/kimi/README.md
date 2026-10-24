# HB-04 Kimi CLI/ACP probe

This metadata harness uses no business data and starts **no external command**
without `KIMI_ALLOW_NETWORK=1`, including version commands. Network opt-in is
permission to run the fixed profiles, not an OS network sandbox. No real ACP
request adapter is implemented; protocol events remain blocked/unknown.

```sh
experiments/harness/kimi/run.sh /tmp/hb04-kimi.json
python3 -m unittest discover -s experiments/harness/kimi -p 'test_*.py' -v
```

The only executable is the reviewed deployment location `/usr/local/bin/kimi`;
profiles are exactly `--version` and `acp --version`. No PATH discovery, shell
parsing or arbitrary adapter commands are supported. `KIMI_CLI`, `KIMI_ACP`,
and `KIMI_CODE_HOME` environment variables are forbidden, including empty values;
the probe reports blocked without echoing their values. Installing Kimi at that
location is a separate deployment step; the probe does not install or download it.

Each run creates a fresh private directory using exclusive `mkdtemp` beneath the
resolved system `/tmp`, explicitly sets mode 0700, verifies ownership/type/mode
and rejects symlink components. It deletes the directory on exit. Caller-supplied
shared directories and symlinks are never adopted or chmodded. Children receive
only a fixed minimal environment with HOME, KIMI_CODE_HOME and XDG/TMP paths
pointing inside that private directory; credentials and loader/proxy variables
are not inherited.

Only with explicit authorization, run:

```sh
KIMI_ALLOW_NETWORK=1 experiments/harness/kimi/run.sh /tmp/hb04-kimi-network.json
```

This attempts only version metadata, without credentials or real model calls.
Missing binaries produce blocked; an unsupported ACP version flag stays unknown.
The harness records profile names and command digests, never user input argv.
stdout/stderr share a 16 KiB capture budget; timeout or overflow terminates the
process group. Captured output redacts known environment secrets, common secret
fields and bearer tokens. Exception reports use exception type only. Redaction
is best effort for arbitrary unlabelled secrets, so do not supply business data.
Tool suggestions remain fixed data; no shell/file/subagent tool is invoked.
