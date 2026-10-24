# 本地 HB-02 VM 资产复用

本工作目录保存了一套 ignored、已核验的 HB-02 ARM64 VM 资产。涉及 HB-02/HB-11 的真实 QEMU、HVF、镜像或生命周期验证时，先运行本页入口，不要从 `/private/tmp` 猜测资产是否存在，也不要自动下载替代品。

## 先验证资产

```sh
experiments/vm/replay-hb02-local.sh verify
```

该命令逐项校验固定 SHA-256，只读挂载 `experiments/vm/tools/UTM-5.0.8.dmg`，并确认 UTM QEMU 10.0.12 支持 HVF。任一文件缺失或漂移即退出 3；应报告缺失，不得把其他版本静默替换进来。

大文件均被 `.gitignore` 排除，固定位置如下：

- `experiments/vm/tools/`：UTM DMG、QEMU launchers、哈希固定并本机 ad-hoc 签名的 QEMU framework copies、离线重建的 `mke2fs` 1.47.4。
- `experiments/vm/assets/hb02-cache/downloads/`：Alpine 3.24.3 minirootfs 与 `linux-virt` 6.18.58-r0 APK。
- `experiments/vm/assets/hb02-replay/`：已生成的 kernel、initramfs 和 64 MiB 只读 smoke root。
- `experiments/vm/state/hb02-replay-data.raw`：已完成第二次启动的持久数据盘。

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

可直接复用 QEMU launchers、固定 framework copies、UTM DMG、minirootfs、kernel APK、kernel `Image` 和 `mke2fs`。工作目录已经用 Zig 0.17.0、Homebrew Rust 1.99.1 自带的 `rust-src`/vendor 及离线 `build-std` 生成静态 Linux AArch64 `hb-supervisor`；不要回退到普通缺 target 的 Cargo 命令。固定入口是：

```sh
PATH="$PWD/experiments/vm/tools:$PATH" guest/image/build-supervisor.sh
```

产物必须经 `file` 和 `llvm-readelf` 确认为 AArch64、静态链接、无 interpreter/dynamic section、non-executable stack。之后按 `doc/runbooks/hb11-real-runtime.md` 生成全新 candidate 目录。HB-02 smoke binary 或 macOS Mach-O supervisor 不能替代该产物。

`/Applications/UTM.app` 不应存在，也不再是启动条件。launcher 要求明确的 `HB02_QEMU_SYSTEM_FRAMEWORK` 或 `HB02_QEMU_IMG_FRAMEWORK`；产品 helper 只传入 `RuntimeAsset` 校验过的 system framework 路径。UTM sibling frameworks 仍从只读挂载包的固定 rpath 加载，这一边界必须随候选证据保留。
