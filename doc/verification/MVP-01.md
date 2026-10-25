# MVP-01 冻结 MVP 组合与验收入口

日期：2026-10-25。工作区基点 `6e153b1837629020face30b1e6e0344d09abb172`，唯一写者，无委派、无提交。本文只证明配置、契约和离线夹具检查，不证明真实模型、VM、Linux CLI 或发布验收通过。

## 唯一组合与官方依据

唯一候选 ID 为 `builtin-kimi-k3-1-pro-csv`：Havenbox 内置 Agent、一个在线模型和固定 CSV CLI。外部 harness、本地模型、MCP 和多会话不进入 MVP manifest、兼容矩阵或发布资格；旧 `config/runtime-candidates.json` 与 HB 证据原样保留作历史输入。

| 字段 | 冻结值 | 官方来源 |
| --- | --- | --- |
| provider | Moonshot AI Kimi API | [API 概述](https://platform.moonshot.cn/docs/api) |
| modelId | `kimi-k3.1-pro` | [快速开始](https://platform.moonshot.cn/docs/guide/start-using-kimi-api)、[Chat Completions](https://platform.moonshot.cn/docs/api/chat) |
| endpoint | `https://api.moonshot.cn/v1/chat/completions`，HTTPS POST | [API 概述](https://platform.moonshot.cn/docs/api)、[Chat Completions](https://platform.moonshot.cn/docs/api/chat) |
| protocol | OpenAI 兼容 Chat Completions | [API 概述](https://platform.moonshot.cn/docs/api) |
| 工具 | function tools，JSON Schema，`strict=true` | [工具调用](https://platform.moonshot.cn/docs/api/tool-use) |
| 结构化计划 | `response_format={"type":"json_schema",...}` | [Chat Completions](https://platform.moonshot.cn/docs/api/chat) |
| 流式 | SSE，`stream_options.include_usage=true` | [Chat Completions](https://platform.moonshot.cn/docs/api/chat) |
| 输出预算 | 本地 `maxOutputTokens=2048`，请求字段 `max_completion_tokens` | [Chat Completions](https://platform.moonshot.cn/docs/api/chat) |
| 推理参数 | `reasoning_effort=low`，允许冻结为 `low`/`high`/`max` | [快速开始](https://platform.moonshot.cn/docs/guide/start-using-kimi-api)、[Chat Completions](https://platform.moonshot.cn/docs/api/chat) |

`contextTokens=8192` 和输出 2048 是 Havenbox 本地预算，不冒充提供方上下文上限。temperature、top_p、presence_penalty、frequency_penalty 不发送；配置不接受任意 provider、endpoint、模型或参数。官方页面只证明文档声明，未发起任何计费请求，也未读取或输出凭据；真实可用性、计划质量和停止行为仍由 MVP-04/07/08 验证。

## 资产、样例和资格

manifest 固定 QEMU/qemu-img 10.0.12、kernel 6.18.58-r0、Alpine 3.24.3 `hb11-candidate-16` root/initramfs/data、Supervisor 0.1.0，以及 Swift 6.3.3、Rust/Cargo 1.99.1、Python 3.15.0、SDK 26.5、Zig 0.17.0、e2fsprogs 1.47.4。已有 QEMU launcher、kernel、三个镜像、Supervisor、CLI template 和 SQLite 源码均记录 SHA-256；Zig/e2fsprogs 可执行文件、Linux CSV/CLI executable、内置 Agent、网关、完整 SBOM/许可证及 QEMU sibling frameworks 仍有明确缺口，保持 `null`/`unresolved`，普通资格默认拒绝。

固定样例绑定 `tests/fixtures/csv/valid-sales.csv`、`duplicate-keys.csv` 和 `expected-results.json` 的实际 SHA-256。期望锚点为 5 行总额 700.00；排除 1 个负数后 4 行 720.00；North 3 行 370.00、South 2 行 330.00；`c-001` 在第 1/2 行重复且第 4 行为空键。CSV oracle 不等于真实端到端验收。

资源上限集中在 manifest `limits`，恢复为单工作空间、单活动任务、只读历史；未完成任务标记 `Interrupted`，不重放模型请求或工具调用、不继续对话。发布资格只接受同一 binding 的 `MVP-07` 和 `MVP-08` receipt；HB-10/11/13/14/16 仅为候选输入，开发 ValidationRunGrant 永远不能进入发布。

## 独立 Review 发现与修复

1. 兼容矩阵 schema 原先只有 `minItems=1` 和 `uniqueItems`，与 protocol 声称拒绝第二组合不一致。受控修复前探针确认：追加一个仅 `reason` 不同的合法组合仍可通过 `check_schema`，证明并非完全重复对象才会漏过。现增加 `maxItems=1`，离线校验器同步支持并执行该关键字；修复后同一探针以 `$.combinations: too many items` 拒绝。`test_mvp_combination_and_paused_capabilities_fail_closed` 保留内容不同的第二组合并直接检查 schema，不依赖 `uniqueItems` 或 `check_inventory` 兜底。
2. `Sources/HavenboxCore/Configuration.swift` 的资格注释残留 HB17/HB31。现改为 MVP-04 负责内置 Agent/网关、MVP-07/MVP-08 负责发布资格；只改注释，不改运行逻辑。schema、校验器、契约测试的 SHA-256 及包含该 Swift 文件的 `sourceSnapshot` 已重算同步。

## 实际命令与退出码

| 命令 | 退出码 | 结果 |
| --- | ---: | --- |
| `python3 scripts/check-baseline.py` | 0 | 23 项契约测试通过；含外部 harness/MCP/本地路由/不同内容第二组合 schema/错误证据/grant 发布负例 |
| `python3 tests/fixtures/csv/check.py` | 0 | 10 个 case、9 个 CSV hash；700.00/720.00 等期望一致，唯一组合仍未获资格 |
| `git diff --check` | 0 | 无空白错误 |
| `python3 scripts/check-development-plan.py` | 1（中间）/0（复验） | 首次因 README 链接时本文件尚未创建而失败；创建后 8 任务、24 需求、链接和 DAG 通过 |
| `arch -arm64 python3 scripts/engineering.py check` | 1（中间）/0（复验） | 首次同样停在缺失证据链接；复验完成 Swift/Rust 构建、宿主测试、Cargo 测试、23 项契约、计划和 CSV 检查，全部通过 |

未验证边界：真实 Moonshot 请求和模型能力、真实文件导入、真实 VM 中 CSV/CLI、Linux 制品构建、停止/崩溃恢复、完整资源耗尽、参考设备、内部包和用户验收均未在本任务执行。没有运行 dsh，也没有修改 dsh 现场；若完整公共门暴露其既有竞态，只记录，不通过修改暂停项消除。
