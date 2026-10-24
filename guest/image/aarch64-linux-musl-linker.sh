#!/bin/bash
set -eu

args=()
for arg in "$@"; do
    # Zig's bundled LLD does not implement this GNU ld workaround. The fixed
    # guest CPU is not affected, and every other rustc hardening flag remains.
    if [[ "$arg" == '-Wl,--fix-cortex-a53-843419' ]]; then
        continue
    fi
    # build-std names the CRT files supplied by a precompiled Rust target,
    # which is intentionally absent. Let Zig inject its matching musl CRT.
    case "$arg" in
        -nostartfiles|crt1.o|crti.o|crtbegin.o|crtend.o|crtn.o) continue ;;
    esac
    args+=("$arg")
done

exec /opt/homebrew/bin/zig cc -target aarch64-linux-musl "${args[@]}"
