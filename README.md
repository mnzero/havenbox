# havenbox
安全、易用的桌面 Agent 工作空间。敏感凭证不进入隔离环境。

当前 MVP 只交付一条可验证链路：内置 Agent 使用 Moonshot AI Kimi API 的 `kimi-k3.1-pro`，经 OpenAI 兼容 `https://api.moonshot.cn/v1/chat/completions`，在无网卡 Linux 虚拟机中调用确定性 CSV CLI。用户明确批准模型可见的数据和工具计划，检查成果后再导出。Kimi CLI、Pi、DeepSeek Harness、本地模型、MCP 和多会话已移到 MVP 之后；当前只是 MVP-01 唯一候选基线，真实模型、VM、CLI 与发布验收仍未完成。

## 项目文档

- [MVP 产品需求文档](doc/product-requirements.md)
- [产品与竞品调研](doc/research-report.md)
- [UI 与用户动线设计](doc/ui-ux-design.md)
- [V0.5 历史原型与检查记录](doc/prototypes/README.md)
- [MVP 技术方案](doc/technical-design.md)
- [MVP 方案复审](doc/technical-review.md)
- [MVP 执行计划](doc/development-tasks.md)

## 现有候选开发工程

仓库已有 SwiftUI 信息窗口、Swift 核心、helper、Rust guest workspace、存储、VM、CSV 引擎、CLI 和配置候选。HB-11 的固定 synthetic validation profile 已在真实 QEMU/HVF guest 中闭环。这些候选尚未组成 MVP 端到端产品，普通任务入口和发布资格保持关闭。

### 主链测试门

每个 HB 任务编码完成后先运行公共检查入口（报告目录必须全新）：

```sh
python3 scripts/test-environment.py --report-dir .build/test-environment/run-N
# 显式允许不使用网络的宿主进程机制测试在 Seatbelt 外运行：
python3 scripts/test-environment.py --host-process-tests --report-dir .build/test-environment/run-N-host
# 当前精确地址过滤不可用：只验证 sandbox 后 blocked，不启动 harness 或宿主进程测试：
python3 scripts/test-environment.py --loopback --report-dir .build/test-environment/run-N-loopback
```

默认及 `--loopback` 均拒绝全部网络；`--loopback` 不是启用本机协议 fixture 的许可，和 `--host-process-tests` 合用也会提前 blocked。独立门禁回归的 TCP fixture 仅验证受控的 127.0.0.1/本机 en0 地址可达性与 sandbox 拒绝，不代表真实 harness 的 loopback 隔离已可用。

退出 `0` 为已覆盖检查通过，`1` 为失败，`2` 为仍有 blocked；不得把 `2` 当作产品验收通过。入口保留每条命令、版本、退出码与日志，清理私有 HOME/临时数据库/构建缓存，不安装依赖；真实 VM、Keychain、模型的前置条件与限制见 [测试环境验证](doc/verification/test-environment.md)。

### 固定构建环境与命令

Apple Silicon macOS；预先准备 `config/build-toolchain.json` 的精确工具链：Apple Swift 6.3.3 (swiftlang-6.3.3.1.3)、Rust/Cargo 1.99.1 Homebrew 构建、Python 3.15.0、macOS SDK 26.5；Linux guest 资产构建另固定 Zig 0.17.0 和 e2fsprogs 1.47.4。宿主工程脚本校验其使用的版本，缺失/不匹配直接失败，**不安装、不升级、不下载**。SDK 默认 `/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk`；可通过 `SDKROOT` 指定同版本目录，通过 `HB_SWIFTC` 指定同版本编译器。macOS 14 仅部署目标，未验最低系统。

```sh
python3 scripts/engineering.py build    # 实编 Swift app/core/helper/测试体，Rust host-native release
python3 scripts/engineering.py check    # build + Swift可执行单测 + Cargo单测 + 契约/计划/CSV检查
python3 scripts/engineering.py package  # check + 全新 dist/Havenbox-engineering 开发目录
open dist/Havenbox-engineering/Havenbox.app  # 仅候选信息窗口，非产品功能验收
```

包目录已存在时拒绝覆盖；请先自行归档旧产物。Swift 产物位于 `.build/engineering/`，Rust 位于 `guest/target/release/`；内部开发包有文件 SHA-256 清单，不含 VM、模型或 Linux 镜像，也不是可分发产品或 SBOM 审批证明。Rust 本机编译目标为 `aarch64-apple-darwin`，**不是 Linux guest 构建通过**。

本机 CLT 的 `swift test` 在 SwiftPM 启动期缺符号退出 134，且无 XCTest；实际入口使用 `swiftc` 与标准库可执行回归，不宣称 XCTest 或 SwiftPM 通过。`Package.swift` 是健康 SwiftPM 环境的备用描述，尚未验证；无外部 Swift 包，所以不伪造 `Package.resolved`。HB-10 固定 SQLite 3.51.3 官方 C amalgamation，不链接系统 SQLite，构建不下载。来源与 hash 见 `Sources/CSQLite/NOTICE.md`，存储验收及未验证边界见 [HB-10](doc/verification/HB-10.md)。Rust workspace 由 `guest/Cargo.lock` 锁定，并使用 `--locked --offline` 构建。

CI 使用固定 SHA 的 checkout action，并显式 checkout 事件 SHA（`github.sha`）构建当前事件提交；专用预配置 runner `hb09-toolchain-1` 执行同一入口和版本检查，未提供该 runner 时会排队。源码基点和未提交快照绑定记录在 `config/runtime-manifest.json`。快照覆盖工程源码、工具链与构建入口，排除生成物和自引用字段。真实 VM 和网络检查属于参考 Mac 集成任务，不以模拟结果通过。详见 [HB-09 实测与边界](doc/verification/HB-09.md)。

### HB-11 inspect-boot 候选检查

`hb-supervisor --inspect-boot` 只读取启动拓扑，不启动任务；非 Linux 明确返回 78。固定 guest profile 已按真实 Alpine kernel 拓扑校准并在额外块设备场景 fail closed，但它仍不是通用硬件认证或执行资格。

`hb11-candidate-16` 已真实运行三条 virtio-serial 通道、单调 host/guest lease、cgroup-before-exec、namespace/chroot/降权、固定 seccomp、角色文件隔离、每流 1 MiB 上限和正常或强杀清理。复用入口见 [HB-02 本地资产](doc/runbooks/hb02-local-vm-assets.md) 与 [HB-11 真实复验](doc/runbooks/hb11-real-runtime.md)。剩余边界包括 sibling UTM frameworks 的逐文件绑定、各类 cgroup 和 quota 分项超限，以及 MVP 真实文件和模型链路；详见 [HB-11 验证记录](doc/verification/HB-11.md)。

## MVP-01 唯一组合与契约检查

```sh
python3 scripts/check-development-plan.py
python3 scripts/check-baseline.py
python3 tests/fixtures/csv/check.py
```

仅使用 Python 标准库，执行唯一组合、清单/schema、fixture 哈希、MVP-07/08 证据和 ValidationRunGrant 的离线回归；不下载、不调用模型、不启动沙箱。当前只有 `builtin-kimi-k3-1-pro-csv` 一个候选，普通执行和发布资格均关闭；开发 ValidationRunGrant 不能进入发布。固定样例引用 `valid-sales.csv` 与 `duplicate-keys.csv`，锚定总额 700.00、排除负数后 720.00、分组和重复键结果。详见 [MVP-01 证据](doc/verification/MVP-01.md)、[协议与领域契约](contracts/protocol.md) 和 [正式构建依赖清单](doc/build-dependencies.md)。旧 `config/runtime-candidates.json` 与 HB 证据仅作历史输入保留。

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
