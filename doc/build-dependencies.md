# MVP 构建依赖清单

机器清单以 [`config/runtime-manifest.json`](../config/runtime-manifest.json) 为准。MVP-01 已将依赖和发布资格收窄到本页列出的唯一组合；旧外部 harness、本地模型和 MCP 只保留在历史候选文件中。

本页定义依赖的交付位置和冻结条件。它不是安装脚本，也不证明产品基线已经通过。实际编译环境见 [`config/build-toolchain.json`](../config/build-toolchain.json)。工程检查入口为 `python3 scripts/engineering.py check`。

## MVP 依赖

| 交付位置 | 依赖 | 当前状态和冻结条件 |
| --- | --- | --- |
| 构建机 | Swift、macOS SDK、Rust、Python、Zig 和 e2fsprogs | 已有固定候选版本；需要保留版本检查、离线构建和来源记录，Zig/e2fsprogs 用于 Linux guest 资产 |
| 宿主包 | SwiftUI、AppKit 和 Keychain 系统 API | 最低系统为候选 macOS 14；需要在参考设备实测 |
| 宿主包 | QEMU、`qemu-img`、HVF entitlement 和 watchdog | 已有 UTM QEMU 候选证据；需要冻结二进制、动态依赖、entitlement、许可证和摘要 |
| 宿主包 | SQLite 3.51.3 | 已固定官方 C 源码；需要保留 source ID、摘要、WAL 和 backup 回归 |
| 宿主包 | 推理网关和内置 Agent | 需要冻结协议、端点、模型、请求限制、制品摘要和许可证决定 |
| 来宾镜像 | Linux kernel、只读 rootfs 和 Supervisor | 已有真实 VM 候选；需要冻结镜像、UID、cgroup、seccomp、namespace、许可证和 SBOM |
| 来宾镜像 | CSV 引擎和 CLI | 已有宿主原生候选；需要构建 Linux 制品，并验证真实 VM 成果链 |
| 检查环境 | Python 标准库 | 当前固定 Python 3.15.0，无 PyPI 依赖 |

任务运行时不得下载或更新依赖。每个制品必须记录来源、版本、SHA-256、许可证和 SBOM 归属。缺少证据的字段保持未知，并阻止普通任务资格。

## MVP 后依赖

以下依赖不进入 MVP 镜像或内部包：

- `llama.cpp`、Metal、本地模型、模板和量化文件。
- Kimi CLI、Pi、dsh 及其 Node、Python、适配器和补丁。
- MCP client、MCP server 和 SDK。
- OAuth、远程连接器、签名、公证和自动更新组件。

保留这些候选项的配置和历史证据，不代表 MVP 构建需要安装它们。MVP 发布检查必须拒绝未声明的运行时依赖。

## 更新流程

选择或更新依赖时，按以下顺序操作：

1. 更新 immutable revision 和制品 metadata。
2. 重新计算契约文件和源码快照摘要。
3. 运行 schema、构建和离线回归。
4. 对新 binding 执行真实集成验收。
5. 保存新的 receipt，不改写旧证据。

内部包不包括 Developer ID、公证和自动升级。HVF entitlement 仍是运行要求。任何重签都会改变制品摘要，必须重新记录并复验。
