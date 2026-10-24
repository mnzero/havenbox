#!/usr/bin/env bash
set -euo pipefail

label=${HB03_LABEL:-com.havenbox.hb03.lifecycle}
domain="gui/$(id -u)"
base=${HB03_RUNTIME_DIR:-"${TMPDIR:-/tmp}/hb03-launchd"}
plist_dir="${HOME}/Library/LaunchAgents"
plist="${plist_dir}/${label}.plist"
helper=${HB03_HELPER:-"$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/hb03-helper.py"}
lease_file=${HB03_LEASE_FILE:-"${base}/lease"}
state_file=${HB03_STATE_FILE:-"${base}/helper.pid"}
ui_pid=${HB03_UI_PID:-$$}

usage() {
  printf '%s\n' "usage: $0 {render|register|print|kickstart|stop|unload|cleanup}" >&2
  exit 2
}

render() {
  mkdir -p "$plist_dir" "$base"
  cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string><string>${helper}</string>
    <string>--ui-pid</string><string>${ui_pid}</string>
    <string>--lease-file</string><string>${lease_file}</string>
    <string>--state-file</string><string>${state_file}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>AbandonProcessGroup</key><false/>
  <key>StandardOutPath</key><string>${base}/stdout.log</string>
  <key>StandardErrorPath</key><string>${base}/stderr.log</string>
</dict>
</plist>
EOF
  chmod 600 "$plist"
  printf '%s\n' "$plist"
}

register() {
  render >/dev/null
  plutil -lint "$plist"
  launchctl bootout "$domain" "$plist" >/dev/null 2>&1 || true
  launchctl bootstrap "$domain" "$plist"
}

print_job() { launchctl print "${domain}/${label}"; }
kickstart() { launchctl kickstart -k "${domain}/${label}"; }
stop() { launchctl kill SIGTERM "${domain}/${label}"; }
unload() { launchctl bootout "$domain" "$plist"; }
cleanup() {
  launchctl bootout "$domain" "$plist" >/dev/null 2>&1 || true
  rm -f -- "$plist" "$lease_file" "$state_file" "${base}/stdout.log" "${base}/stderr.log"
  # Only remove the runtime directory after deleting this job's known files;
  # unrelated files keep an explicitly supplied directory intact.
  rmdir -- "$base" 2>/dev/null || true
}

case "${1:-}" in
  render) render ;;
  register) register ;;
  print) print_job ;;
  kickstart) kickstart ;;
  stop) stop ;;
  unload) unload ;;
  cleanup) cleanup ;;
  *) usage ;;
esac
