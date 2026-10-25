#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cache="$root/tools/source-cache/e2fsprogs-1.47.4"
output="$root/tools"
sdk=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk
source_date_epoch=1772830800
base_url=https://www.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v1.47.4
# The frozen candidate binaries retain upstream debug file names. Map each
# throwaway source tree to the path used by the accepted prototype so clean
# builds have identical Mach-O bytes without reusing its object files.
canonical_source="$root/tools/.candidate17-prototype-f788/e2fs-source/e2fsprogs-1.47.4"

usage() {
  echo "usage: $0 [--source-cache DIR] [--output DIR]" >&2
  exit 2
}

while (($#)); do
  case "$1" in
    --source-cache) cache=${2:-}; shift 2 ;;
    --output) output=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n "$cache" && -n "$output" ]] || usage

verify_hash() {
  local expected=$1 file=$2
  [[ -f "$file" && ! -L "$file" ]] || { echo "blocked: missing regular file: $file" >&2; exit 3; }
  printf '%s  %s\n' "$expected" "$file" | shasum -a 256 -c -
}

fetch_if_missing() {
  local name=$1
  [[ -e "$cache/$name" ]] && return
  local temporary="$cache/.$name.download"
  curl --fail --location --proto '=https' --tlsv1.2 \
    --output "$temporary" "$base_url/$name"
  mv "$temporary" "$cache/$name"
}

verify_minos() {
  local file=$1
  local actual
  actual=$(otool -l "$file" | awk '$1 == "minos" { print $2 }' | sort -u)
  [[ "$actual" == 14.0 ]] || {
    echo "blocked: $file has minOS '${actual:-missing}', expected 14.0" >&2
    exit 3
  }
}

verify_binary() {
  local file=$1 expected=$2
  verify_hash "$expected" "$file"
  verify_minos "$file"
  codesign --verify --strict "$file"
  [[ "$(otool -L "$file" | tail -n +2 | awk '{print $1}')" == /usr/lib/libSystem.B.dylib ]] || {
    echo "blocked: unexpected dynamic dependency in $file" >&2
    exit 3
  }
  if strings "$file" | grep -E '/opt/homebrew|/Cellar/|@@HOMEBREW|HOMEBREW_PREFIX' >/dev/null; then
    echo "blocked: Homebrew path leaked into $file" >&2
    exit 3
  fi
}

[[ -d "$sdk" ]] || { echo "blocked: pinned macOS SDK not found: $sdk" >&2; exit 3; }
[[ "$(xcrun --sdk "$sdk" --show-sdk-version)" == 26.5 ]] || {
  echo "blocked: pinned SDK does not report version 26.5" >&2
  exit 3
}
mkdir -p "$cache" "$output"
fetch_if_missing e2fsprogs-1.47.4.tar.xz
fetch_if_missing sha256sums.asc
fetch_if_missing e2fsprogs-1.47.4.tar.sign
verify_hash fd5bf388cbdbe006a3d3b318d983b2948382440acc85a87f1e7d108653e8db0b \
  "$cache/e2fsprogs-1.47.4.tar.xz"
verify_hash 0259700d11fce6ef72001e131f1efd714815fef53559555f069539eee9a91cb7 \
  "$cache/sha256sums.asc"
verify_hash ff826cf86828bf3ed8a7fcbe3e5c0c171f8ebfc5f46c43b27adc1ffc2c6a7b9c \
  "$cache/e2fsprogs-1.47.4.tar.sign"
grep -Fx 'fd5bf388cbdbe006a3d3b318d983b2948382440acc85a87f1e7d108653e8db0b  e2fsprogs-1.47.4.tar.xz' \
  "$cache/sha256sums.asc" >/dev/null || {
  echo "blocked: clearsigned checksum content does not pin the source archive" >&2
  exit 3
}

staging=$(mktemp -d "$output/.candidate17-e2fs.XXXXXX")
cleanup() {
  [[ "$staging" == "$output/.candidate17-e2fs."* ]] && rm -rf -- "$staging"
}
trap cleanup EXIT
mkdir "$staging/source"
tar -xf "$cache/e2fsprogs-1.47.4.tar.xz" -C "$staging/source"
source_tree="$staging/source/e2fsprogs-1.47.4"

clean_env() {
  /usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME="$HOME" TMPDIR=/tmp \
    SOURCE_DATE_EPOCH=$source_date_epoch ZERO_AR_DATE=1 LC_ALL=C TZ=UTC \
    SDKROOT="$sdk" MACOSX_DEPLOYMENT_TARGET=14.0 "$@"
}

build_once() {
  local build=$1
  mkdir "$build"
  (
    cd "$build"
    clean_env CC=/usr/bin/clang AR=/usr/bin/ar RANLIB=/usr/bin/ranlib \
      CFLAGS="-O2 -isysroot $sdk -mmacosx-version-min=14.0 -ffile-prefix-map=$source_tree=$canonical_source -fdebug-prefix-map=$source_tree=$canonical_source" \
      CPPFLAGS="-isysroot $sdk" \
      LDFLAGS="-isysroot $sdk -mmacosx-version-min=14.0" \
      "$source_tree/configure" --prefix=/usr/local \
        --enable-libuuid --enable-libblkid \
        --disable-elf-shlibs --disable-bsd-shlibs --disable-profile \
        --disable-fuse2fs --disable-defrag --disable-imager --disable-resizer \
        --disable-uuidd --disable-e2initrd-helper --disable-fsck \
        --disable-nls --disable-rpath --disable-backtrace
    clean_env /usr/bin/make -j4 libs
    clean_env /usr/bin/make -C e2fsck -j4 e2fsck
    clean_env /usr/bin/make -C debugfs -j4 debugfs
    clean_env /usr/bin/make -C misc -j4 mke2fs
  )
}

build_once "$staging/build-1"
build_once "$staging/build-2"

for relative in misc/mke2fs debugfs/debugfs e2fsck/e2fsck misc/mke2fs.conf; do
  cmp "$staging/build-1/$relative" "$staging/build-2/$relative"
done
verify_binary "$staging/build-1/misc/mke2fs" \
  fbaccbd585defb2ea8a4cc887e2a3b0d8eeead1a87aba8c891133397c826976a
verify_binary "$staging/build-1/debugfs/debugfs" \
  da5fd75e391994d52ee0ac939b1bde03811d5ac419fb7548a712e1d0f4415eee
verify_binary "$staging/build-1/e2fsck/e2fsck" \
  e08604ae9518a551aa95ff15b274796148455341a87e1cfac96a92ab25f20d2b
verify_hash c998d64f9b58c2235186f8ee75ba602d1fdb2991d94695fd687ab0e8096b6727 \
  "$staging/build-1/misc/mke2fs.conf"

functional="$staging/functional"
mkdir "$functional"
MKE2FS_CONFIG="$staging/build-1/misc/mke2fs.conf" \
  "$staging/build-1/misc/mke2fs" -q -t ext4 -F "$functional/ext4.raw" 16384
"$staging/build-1/debugfs/debugfs" -w \
  -R "write $source_tree/README /README" "$functional/ext4.raw" >/dev/null
"$staging/build-1/debugfs/debugfs" -R 'cat /README' "$functional/ext4.raw" 2>/dev/null | \
  cmp - "$source_tree/README"
"$staging/build-1/e2fsck/e2fsck" -f -n "$functional/ext4.raw" >/dev/null

gpg_tool=false
for command in gpg gpgv sq; do
  command -v "$command" >/dev/null 2>&1 && gpg_tool=true
done
trusted_keyring=false
[[ -d "$HOME/.gnupg" ]] && trusted_keyring=true
python3 - "$staging/candidate17-e2fsprogs-receipt.json" "$gpg_tool" "$trusted_keyring" <<'PY'
import json
import pathlib
import sys

receipt = {
    "schemaVersion": "havenbox.candidate17-e2fsprogs-receipt.v1",
    "source": "official kernel.org HTTPS",
    "sourceSha256Verified": True,
    "clearsignedChecksumContentMatched": True,
    "gpgToolAvailable": sys.argv[2] == "true",
    "trustedKeyringAvailable": sys.argv[3] == "true",
    "signatureAuthenticityVerified": False,
    "verificationLimit": (
        "GPG verification tooling and a trusted key were unavailable on the candidate-17 "
        "build host; current provenance evidence is limited to official HTTPS, the pinned "
        "source SHA-256, and matching content inside the clearsigned checksum file."
    ),
}
path = pathlib.Path(sys.argv[1])
temporary = path.with_name("." + path.name + ".tmp")
temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
temporary.replace(path)
PY
chmod 0444 "$staging/candidate17-e2fsprogs-receipt.json"

for mapping in mke2fs:misc/mke2fs debugfs:debugfs/debugfs e2fsck:e2fsck/e2fsck \
  mke2fs.conf:misc/mke2fs.conf; do
  name=${mapping%%:*}
  relative=${mapping#*:}
  mv -f "$staging/build-1/$relative" "$output/$name"
done
mv -f "$staging/candidate17-e2fsprogs-receipt.json" \
  "$output/candidate17-e2fsprogs-receipt.json"
chmod 0555 "$output/mke2fs" "$output/debugfs" "$output/e2fsck"
chmod 0444 "$output/mke2fs.conf"

echo "published reproducible candidate-17 e2fsprogs tools in $output"
