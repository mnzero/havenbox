#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
rust_sysroot=$(rustc --print sysroot)
vendor="$rust_sysroot/lib/rustlib/src/rust/library/vendor"
linker="$root/guest/image/aarch64-linux-musl-linker.sh"

[[ $(rustc --version) == 'rustc 1.99.1 (58cf8f9c3 2026-10-13) (Homebrew)' ]] || {
  echo 'blocked: expected Homebrew Rust 1.99.1' >&2
  exit 3
}
[[ $(/opt/homebrew/bin/zig version) == '0.17.0' && -d "$vendor" && -x "$linker" ]] || {
  echo 'blocked: pinned Zig, Rust vendor source, or linker wrapper missing' >&2
  exit 3
}

RUSTC_BOOTSTRAP=1 \
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_MUSL_LINKER="$linker" \
cargo \
  --config 'source.crates-io.replace-with="vendored-sources"' \
  --config "source.vendored-sources.directory=\"$vendor\"" \
  -Z build-std=std,panic_abort build \
  --manifest-path "$root/guest/Cargo.toml" \
  --target aarch64-unknown-linux-musl --release --locked --offline

artifact="$root/guest/target/aarch64-unknown-linux-musl/release/hb-supervisor"
file "$artifact"
/opt/homebrew/opt/llvm/bin/llvm-readelf -l -d "$artifact"
