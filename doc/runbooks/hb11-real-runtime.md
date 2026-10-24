# HB-11 真实运行时复验

本页用于复用工作目录内已核验的 HB-02 工具和缓存，重建并运行 HB-11 固定 synthetic guest。它不开放普通用户执行，不签发 ValidationRunGrant，也不替代 HB-31/32/33。

## 资产入口

先读 [本地 HB-02 VM 资产复用](hb02-local-vm-assets.md)，然后运行：

```sh
experiments/vm/replay-hb02-local.sh verify
```

大文件位于 `.gitignore` 覆盖的 `experiments/vm/tools/`、`experiments/vm/assets/hb02-cache/` 和 `experiments/vm/assets/hb11-candidate-*`。不要因 Git 未跟踪它们而重新下载，也不要把其他镜像静默替换进来。当前可复验组合为 UTM 5.0.8 / QEMU 10.0.12、Alpine 3.24.3、kernel 6.18.58-r0、Zig 0.17.0 和 Homebrew Rust 1.99.1；每次仍须按 manifest/hash 核对。QEMU launcher 与 ad-hoc-signed framework copy 由 `experiments/vm/build-utm-launchers.sh` 生成，运行时不使用 `/Applications/UTM.app` 符号链接。

## 重建 guest

静态 Linux AArch64 supervisor 使用 `guest/image/aarch64-linux-musl-linker.sh` 调用固定本机 Zig，并由 Homebrew Rust 自带的 `rust-src` 与 vendor 源离线 `build-std`。

```sh
guest/image/build-supervisor.sh
```

命令必须退出 0。`file` 输出必须包含 `ARM aarch64`、`statically linked` 和 `stripped`。`llvm-readelf` 输出不得包含 `INTERP` 或 `DYNAMIC`，`GNU_STACK` 不得有执行权限。

镜像入口是：

```sh
PATH="$PWD/experiments/vm/tools:$PATH" guest/image/build-image.sh \
  --minirootfs experiments/vm/assets/hb02-cache/downloads/alpine-minirootfs-3.24.3-aarch64.tar.gz \
  --kernel-apk experiments/vm/assets/hb02-cache/downloads/linux-virt-6.18.58-r0.apk \
  --supervisor guest/target/aarch64-unknown-linux-musl/release/hb-supervisor \
  --output experiments/vm/assets/hb11-candidate-N \
  --debugfs experiments/vm/tools/debugfs \
  --e2fsck experiments/vm/tools/e2fsck
```

输出目录必须是新目录。脚本校验上游 hash、ext4 一致性和所有可见 inode 的 0:0 所有权，失败时不会发布部分目录，并将四个镜像产物设为 `0444`。运行时必须把 data 模板复制成单独的 `0600` 文件。

## 验证 guest 通道和隔离

先为每次探针创建新的 data 克隆。不要让两次 QEMU 启动共享可写 data 文件。

```sh
image="$PWD/experiments/vm/assets/hb11-candidate-16"
new_data() {
  local directory
  directory=$(mktemp -d "$HOME/hb11-probe.XXXXXX")
  chmod 0700 "$directory"
  cp -c "$image/data.raw" "$directory/data.raw"
  chmod 0600 "$directory/data.raw"
  printf '%s\n' "$directory/data.raw"
}
python3 experiments/vm/probe-hb11-channel.py "$image" "$(new_data)" harness normal
python3 experiments/vm/probe-hb11-channel.py "$image" "$(new_data)" worker normal
python3 experiments/vm/probe-hb11-channel.py "$image" "$(new_data)" harness lease-expiry
python3 experiments/vm/probe-hb11-channel.py "$image" "$(new_data)" harness replay
python3 experiments/vm/probe-hb11-channel.py "$image" "$(new_data)" harness device-drift
python3 experiments/vm/probe-hb11-channel.py "$image" "$(new_data)" harness output-overflow
python3 experiments/vm/probe-hb11-channel.py "$image" "$(new_data)" harness active-lease-expiry
```

两个正常路径必须输出 `HelloAck`、`SyntheticTaskCompleted` 和 `Stopped`。终态探针必须分别输出 `lease-terminal=true`、`replay-terminal=true`、`device-drift-rejected=true`、`output-overflow-killed=true` 和 `active-lease-killed=true`。

## 验证产品启动链

构建 Swift 工程和 `tests/RuntimeIntegration/RuntimeIntegration.swift` 后，运行完整 `RuntimeManager → launchd → helper → QEMU → guest`：

```sh
SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk \
  MACOSX_DEPLOYMENT_TARGET=14.0 python3 scripts/engineering.py build
swiftc -sdk /Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk \
  -target arm64-apple-macosx14.0 -swift-version 6 -warnings-as-errors \
  -parse-as-library -I Sources/CSQLite/include -I .build/engineering \
  tests/RuntimeIntegration/RuntimeIntegration.swift \
  .build/engineering/libHavenboxCore.a .build/engineering/sqlite3.o \
  -o .build/engineering/runtime-integration
chmod 0555 .build/engineering/havenbox-helper .build/engineering/runtime-integration
image="$PWD/experiments/vm/assets/hb11-candidate-16"
experiments/vm/run-hb11-runtime.sh "$image" normal
experiments/vm/run-hb11-runtime.sh "$image" lease-expiry
```

脚本重新核对 candidate manifest 和每个启动资产哈希。脚本创建一个 `0700` 私有目录和一个 `0600` data 克隆。正常路径必须输出 `HB-11 real RuntimeManager/launchd/helper/QEMU integration passed`。lease 路径必须输出两次 `stage=lease-rejected` 和 `HB-11 real lease expiry remained terminal`。

## 验证强制终止清理

分别强制终止 UI driver、helper 和 QEMU。每次运行都使用新的 instance 和 data 克隆。

```sh
image="$PWD/experiments/vm/assets/hb11-candidate-16"
experiments/vm/verify-hb11-kill.sh "$image" ui
experiments/vm/verify-hb11-kill.sh "$image" helper
experiments/vm/verify-hb11-kill.sh "$image" qemu
```

脚本只向当前运行目录对应的进程发送 `SIGKILL`。它从 `launch.json` 读取 instance，并核对 launchd label 和父子 PID。每次成功都输出 `cleanup=true`，并保留日志和运行目录供复核。

## 检查没有残留

运行以下命令。两个搜索命令都不得输出 HB-11 job 或进程。

```sh
launchctl print "gui/$(id -u)" 2>/dev/null | rg 'dev\.havenbox\.runtime\.' || true
ps -axo pid=,command= | rg '/(havenbox-helper|qemu-system-aarch64)( |$)' || true
```

再检查强杀脚本输出的运行目录。目录中不得存在 `launch.json`、`launchd.plist`、`ui-control.fifo`、`control.sock`、`file.sock` 或 `inference.sock`。
