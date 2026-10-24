# havenbox
安全、易用的桌面 Agent 沙箱，支持主流 Agent 与 MCP，敏感凭证无需进入沙箱。

本期计划接入 Kimi CLI、Pi、DeepSeek Harness（dsh），支持多轮会话与历史恢复、独立配置本地或联网推理模型，并接入本地 MCP 与 CLI 工具。联网推理通过宿主网关和显式数据授权，真实模型凭据不进入隔离空间。具体组合和验收见产品需求文档。

## 项目文档

- [产品需求文档](doc/product-requirements.md)
- [产品与竞品调研](doc/research-report.md)
- [UI 与用户动线设计](doc/ui-ux-design.md)
- [V0.5 可点击原型与检查记录](doc/prototypes/README.md)
- [技术方案](doc/technical-design.md)
- [技术方案对照评审](doc/technical-review.md)
- [开发任务与验收计划](doc/development-tasks.md)

## HB-09 候选开发工程

已提供 SwiftUI 信息窗口、Swift 核心/header 预检与 helper 可执行体、Rust guest workspace（protocol/supervisor/adapters）。**不是可执行产品**：无 VM、推理、存储或文件处理，helper/supervisor 仅 `--version` 成功，其余参数明确退出 78；不签发 ValidationRunGrant，也不授予普通组合运行资格。

### 固定构建环境与命令

Apple Silicon macOS；预先准备 `config/build-toolchain.json` 的精确工具链：Apple Swift 6.3.3 (swiftlang-6.3.3.1.3)、Rust/Cargo 1.99.1 Homebrew 构建、Python 3.15.0、macOS SDK 26.5。构建脚本校验版本，缺失/不匹配直接失败，**不安装、不升级、不下载**。SDK 默认 `/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk`；可通过 `SDKROOT` 指定同版本目录，通过 `HB_SWIFTC` 指定同版本编译器。macOS 14 仅部署目标，未验最低系统。

```sh
python3 scripts/engineering.py build    # 实编 Swift app/core/helper/测试体，Rust host-native release
python3 scripts/engineering.py check    # build + Swift可执行单测 + Cargo单测 + 契约/计划/CSV检查
python3 scripts/engineering.py package  # check + 全新 dist/Havenbox-engineering 开发目录
open dist/Havenbox-engineering/Havenbox.app  # 仅候选信息窗口，非产品功能验收
```

包目录已存在时拒绝覆盖；请先自行归档旧产物。Swift 产物位于 `.build/engineering/`，Rust 位于 `guest/target/release/`；内部开发包有文件 SHA-256 清单，不含 VM/模型/harness 或 Linux 镜像，不是可分发产品/SBOM 审批证明。Rust 本机编译目标为 `aarch64-apple-darwin`，**不是 Linux guest 构建通过**。

本机 CLT 的 `swift test` 在 SwiftPM 启动期缺符号退出 134，且无 XCTest；实际入口使用 `swiftc` 与标准库可执行回归，不宣称 XCTest/SwiftPM 通过。`Package.swift` 是健康 SwiftPM 环境的备用描述，尚未验证；无外部 Swift 依赖所以不伪造 `Package.resolved`。Rust 三个本地 crate 由 `guest/Cargo.lock` 锁定，`--locked --offline` 构建。

CI 使用固定 SHA 的 checkout action，并显式 checkout 事件 SHA（`github.sha`）构建当前事件提交；专用预配置 runner `hb09-toolchain-1` 执行同一入口和版本门禁，未提供该 runner 时会排队而非降级到未知工具链。源码起点与未提交快照绑定记录在 runtime manifest：起点是 `bd98c68c07e8be387cc81c1d1a843d3400554140`，快照摘要覆盖实际工程源码、工具链与构建入口，排除生成物和自引用字段；它不是未来提交 hash，也不表示源码已提交。真实 VM/网络/GPU 检查是单独参考 Mac 集成任务，当前显式退出 78，不以 mock 通过。详见 [HB-09 实测与边界](doc/verification/HB-09.md)。

## HB-08 候选契约检查

```sh
python3 scripts/check-baseline.py
```

仅使用 Python 标准库，执行清单/schema、证据失效和 ValidationRunGrant 的离线回归；不下载、不调用模型、不启动沙箱。通过不代表产品验收或基线冻结；当前所有候选组合均不可供普通用户执行。详见 [HB-08 证据](doc/verification/HB-08.md)、[协议与领域契约](contracts/protocol.md) 和 [正式构建依赖清单](doc/build-dependencies.md)。

## AI 编程协作

共用规范维护在 [AGENTS.md](AGENTS.md)，不依赖开发者个人的全局配置。

| 工具 | 仓库入口 |
| --- | --- |
| Codex、Cursor | 根目录 `AGENTS.md` |
| Claude Code | `CLAUDE.md` 导入 `AGENTS.md` |
| Gemini CLI | `GEMINI.md` 导入 `AGENTS.md` |
| GitHub Copilot | `.github/copilot-instructions.md` 指向 `AGENTS.md` |
| 其他 agent | 开始任务时显式要求读取 `AGENTS.md` |

使用新会话检查工具是否加载了规范；工具版本、配置及功能模式可能影响加载方式。以上入口提供指令上下文，实际文件、网络和命令权限仍由运行环境控制。

## Git 提交规范

采用 Conventional Commits，格式为 `type(scope): 描述`。启用命令、类型列表与校验方式见[提交规范](doc/commit-conventions.md)。

## VM 模块

HB-02 smoke guest 的 initramfs 加载六个模块：`virtio_blk`、`qemu_fw_cfg`、`crc16`、`mbcache`、`jbd2` 和 `ext4`。
