# 本地 HB-02 VM 资产复用

本工作目录保存了一套 ignored、已核验的 HB-02 ARM64 VM 资产。涉及 HB-02/HB-11 的真实 QEMU、HVF、镜像或生命周期验证时，先运行本页入口，不要从 `/private/tmp` 猜测资产是否存在，也不要自动下载替代品。

## 先验证资产

```sh
experiments/vm/replay-hb02-local.sh verify
```

该命令逐项校验固定 SHA-256（包括 `mke2fs.conf`），只读挂载 `experiments/vm/tools/UTM-5.0.8.dmg`，并确认 UTM QEMU 10.0.12 支持 HVF。任一文件缺失或漂移即退出 3；应报告缺失，不得把其他版本静默替换进来。工具通过后若仍缺 HB-02 镜像，校验会继续并准确报告第一个缺失的镜像资产，不会把工具链误报为门槛。

大文件均被 `.gitignore` 排除，固定位置如下：

- `experiments/vm/tools/`：UTM DMG、QEMU launchers、哈希固定并本机 ad-hoc 签名的 QEMU framework copies，以及离线重建的 e2fsprogs 1.47.4（`mke2fs`、`debugfs`、`e2fsck`、`mke2fs.conf`）。
- `experiments/vm/assets/hb02-cache/downloads/`：Alpine 3.24.3 minirootfs 与 `linux-virt` 6.18.58-r0 APK。
- `experiments/vm/assets/hb02-replay/`：已生成的 kernel、initramfs 和 64 MiB 只读 smoke root。
- `experiments/vm/state/hb02-replay-data.raw`：已完成第二次启动的持久数据盘。

candidate-17 于 2026-10-25 从上述两项缓存输入在全新 staging 中重建并原子发布 replay 资产，随后用固定 UTM QEMU 10.0.12/HVF 完成两次 smoke 启动和一次 kernel probe。冻结摘要为：`Image` `849394914e6a93cc9bb17e901f445ce21fcd7e4528541d9496a74d6a6ccee319`、`initramfs-hb02` `8c6c30aaf4737646496a273cc0cafdb5a7f96de25e23da8cdf8973d479e9b530`、`root.raw` `341c74a04a9a88f94a9987809490170d8eba497d3598b2329458724313c2728d`、`hb02-replay-data.raw` `ce1809621bb6d9fa9eaa86100b25a45fb3a740cbe0d8d826231802f44f8b3ece`。ignored receipt 位于 `experiments/vm/assets/hb02-replay/candidate17-hb02-replay-receipt.json`，三次完整 QEMU 日志位于 `experiments/vm/state/hb02-replay-run{1,2,3}-qemu.log`。

## 重放真实 HB-02 启动

```sh
experiments/vm/replay-hb02-local.sh start
rg 'HB02_GUEST_PROBE|HB02_GUEST_READY|HB02_READY' experiments/vm/state/qemu.log
experiments/vm/replay-hb02-local.sh stop
```

成功标准是随机 nonce 握手、`root_readonly=true`、`network=loopback-only`、`shared_mounts=none` 和 `persistence=present`。`stop` 必须报告 verified process group 已停止，且 `experiments/vm/state/qemu.pid` 不再存在。该 smoke 证明真实 QEMU/HVF、只读根盘、独立持久盘和基础生命周期；不证明 HB-11 supervisor、三条 virtio-serial 通道、seccomp、quota 或 sandbox。

需要单独复核固定 kernel 的 HB-11 原语时：

```sh
experiments/vm/replay-hb02-local.sh kernel-probe-start
rg 'HB11_KERNEL_PROBE|HB02_GUEST_READY|HB02_READY' experiments/vm/state/qemu.log
experiments/vm/replay-hb02-local.sh stop
```

`HB11_KERNEL_PROBE` 成功行证明该次真实 guest 已启用 cgroup v2 `cpu/memory/pids`，完成子进程 admission、`cgroup.kill` 后空组核对、mount/network/IPC namespace 和 UID/GID 10001 降权。这只证明 kernel/Alpine 平台原语可用；它不运行 Rust supervisor，不能用于宣称 supervisor 的 cgroup-before-exec、FD 关闭、seccomp、quota 或租约清理通过。

## 用于 HB-11

可直接复用 QEMU launchers、固定 framework copies、UTM DMG、minirootfs、kernel APK、kernel `Image` 和固定 e2fsprogs 工具组。工作目录已经用 Zig 0.17.0、Homebrew Rust 1.99.1 自带的 `rust-src`/vendor 及离线 `build-std` 生成静态 Linux AArch64 `hb-supervisor`；不要回退到普通缺 target 的 Cargo 命令。固定入口是：

```sh
PATH="$PWD/experiments/vm/tools:$PATH" guest/image/build-supervisor.sh
```

产物必须经 `file` 和 `llvm-readelf` 确认为 AArch64、静态链接、无 interpreter/dynamic section、non-executable stack。之后按 `doc/runbooks/hb11-real-runtime.md` 生成全新 candidate 目录。HB-02 smoke binary 或 macOS Mach-O supervisor 不能替代该产物。

`/Applications/UTM.app` 不应存在，也不再是启动条件。launcher 要求明确的 `HB02_QEMU_SYSTEM_FRAMEWORK` 或 `HB02_QEMU_IMG_FRAMEWORK`；产品 helper 只传入 `RuntimeAsset` 校验过的 system framework 路径。UTM sibling frameworks 仍从只读挂载包的固定 rpath 加载，这一边界必须随候选证据保留。

## 重建固定本机工具

先用 `replay-hb02-local.sh mount` 从固定 SHA-256 的 UTM 5.0.8 DMG 建立只读挂载，再分别运行：

```sh
experiments/vm/build-utm-launchers.sh /Volumes/UTM/UTM.app
experiments/vm/build-pinned-e2fsprogs.sh
```

两条脚本均固定 SDK 26.5 和 deployment target 14.0，在两个洁净构建目录生成产物，只有 `cmp` 与冻结哈希同时通过后才逐文件原子发布至 `experiments/vm/tools/`。两个 QEMU launcher 的 minOS 是 14.0；来自 UTM 的两个 universal framework 保留原件 minOS 12.0，脚本按架构归一化签名 load command、`__LINKEDIT` 签名长度和 signature blob 后，证明其余 Mach-O 字节与 DMG 原件一致。

e2fsprogs 构建固定官方 1.47.4 源码、`SOURCE_DATE_EPOCH`，关闭 NLS 和动态 e2fs 库。构建 receipt 是 ignored 文件 `experiments/vm/tools/candidate17-e2fsprogs-receipt.json`。candidate-17 主机没有 GPG 工具和可信 key，因此当前来源证据仅为 kernel.org 官方 HTTPS、固定源码 SHA-256，以及 clearsigned checksum 中源码条目内容一致；这不等于验证了签名身份真实性。

冻结参数与全部工具摘要见 `experiments/vm/candidate17-toolchain.json`。脚本不会静默接受其他 SDK、UTM framework 或 e2fsprogs 源码。
