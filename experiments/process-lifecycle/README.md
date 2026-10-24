# HB-03 process lifecycle probe

`probe.py` starts real local Python processes and records PID/PGID before and after UI termination, helper termination, and lease expiry. It is a mechanism probe for this task and is not the product runtime or a substitute for a signed helper. The helper keeps its child in one process group and reacts to a dead UI or expired lease; the helper-kill scenario applies the explicit process-group kill that the production user `launchd` job must provide.

Run on macOS with:

```sh
experiments/process-lifecycle/run.sh
```

The probe emits JSON and exits nonzero if any process remains observable after cleanup. It uses no network, secrets, project files, or model/QEMU simulation.

## User launchd job procedure

The production job must be installed as a per-user LaunchAgent, with a generated label and an absolute executable path owned by the user. Before registration, inspect the rendered plist and verify `ProgramArguments`, `UserName` is absent (the user domain supplies identity), `KeepAlive` is absent/false, no `StartInterval`/`StartCalendarInterval` exists, and `AbandonProcessGroup` is `false`. Register and inspect it as the logged-in user:

```sh
label=com.havenbox.runtimewatchdog.<workspace-id>
plist="$HOME/Library/LaunchAgents/$label.plist"
plutil -lint "$plist"
launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl print "gui/$(id -u)/$label"
```

Run the controlled kill scenarios, then unload the job and confirm it is absent. `bootout` is the unload operation; never use `sudo` for a user job:

```sh
launchctl bootout "gui/$(id -u)" "$plist"
launchctl print "gui/$(id -u)/$label" 2>&1; test $? -ne 0
rm -f "$plist"
```

The commands above are operational steps, not evidence by themselves. Evidence requires captured `launchctl print` output, `ps -axo pid,ppid,pgid,stat,command` snapshots, each signal command and exit status, and the absence of descendants after cleanup. This workspace has no product plist or fixed helper, so no real product job was registered.
