# HB-04 至 HB-07 前置侦察报告（harness preflight）

记录日期：2026-10-24。工作区：`harness-preflight`。本报告是 HB-04 至 HB-07 的本机前置侦察，不替代正式任务，也不授予任何 harness、协议或模型组合兼容通过。

探测遵守以下边界：仅执行版本、路径、帮助、导入检查和本地只读清单；Kimi 与 Pi 探测使用临时隔离目录；未发送 prompt、CSV、工具调用或其他业务数据，未登录、安装依赖、联网下载或提交密钥；未启动 VM、模型服务或正式适配器。命令存在、帮助可读或进程能启动都不等于协议、工具、安全边界或组合兼容通过。

## 结果总览

| 项目 | 结果 | 本机证据 | 结论边界 |
|---|---|---|---|
| Kimi CLI 路径与版本 | **pass（仅发现）** | `command -v kimi` → `/Users/mnzero/.kimi-code/bin/kimi`，退出 0；`kimi --version` → `2.2.0`，退出 0 | 尚未验证 ACP 握手、JSON/事件语义、工具调用、停止、会话隔离、无网行为或 VM 内可用性 |
| Kimi CLI 帮助与入口 | **pass（仅帮助）** | `kimi --help` 退出 0；列出 `acp`、`session`、`doctor` 等入口 | 帮助文本不是 ACP 兼容性证据；没有运行 `acp`，没有发送业务数据 |
| Pi 路径与版本 | **pass（仅发现）** | `command -v pi` → `/Users/mnzero/.npm-global/bin/pi`，退出 0；解析目标为 Pi bundle；`pi --version` → `1.3.0`，退出 0 | 尚未验证模型提供商、工具、组合或真实 VM 行为 |
| Pi RPC 帮助/协议文档 | **pass（本地接口资料）** | `pi --help` 退出 0，确认 `--mode rpc`、`--session-dir`、`--no-session`、`--offline`、`--no-mcp`、`--no-tools`、`--no-extensions` 等选项；`docs/rpc.md` 记录 JSONL stdin/stdout、`response`/事件、`prompt`、`abort`、`abort_bash`、`get_state` 等入口 | `pi rpc --help` 实际按未知子命令回退到总帮助（退出 0）；`pi --help-rpc` 退出 1（Unknown option），所以不能把专用帮助命令写成可用。协议仍需真实 RPC 交换证据 |
| Pi 隔离启动烟测 | **pass（仅生命周期）** | `PI_OFFLINE=1 PI_CODING_AGENT_DIR=/tmp/.../pi-config PI_CODING_AGENT_SESSION_DIR=/tmp/.../sessions pi --mode rpc --no-session --no-mcp --no-skills --no-extensions </dev/null`；进程退出 0；只生成临时 `models-store.json`、`auth.json` | 没有发 prompt；未证明 RPC 响应、模型调用、工具入口、取消/停止或数据不出网 |
| dsh CLI/SDK | **blocked** | `command -v dsh` 退出 1；`dsh --version`、`dsh --help` 均退出 127；`python3 -c 'import dsh'` 退出 1（ModuleNotFoundError） | dsh 版本、官方包、profile、SDK JSON-RPC、会话/停止入口均待真实环境补齐；未安装任何依赖 |
| 网络本地模型服务 | **blocked** | `ollama`、`lmstudio`、`llama`、`llama-cli`、`mlx-lm` 均未找到；`ollama --version/list` 均退出 127 | 没有可用本地模型服务或 CLI 候选；不能选择模型 ID、量化、runtime、哈希或许可证 |
| Python 模型依赖 | **blocked** | `import mlx` 退出 1（ModuleNotFoundError） | 不安装未知依赖；HB-07 需要在授权 VM/设备中补运行时与模型制品证据 |
| 本机硬件参考性 | **unknown（仅环境观测）** | `system_profiler SPHardwareDataType` 显示 Apple M5、32 GB；`sysctl` 显示 34359738368 bytes、arm64 | 不是 HB-01 目标 16 GiB 参考设备的证明；仍需在目标 VM/设备记录 OS、硬件、可用内存、架构和资源限制 |

## 逐项探测记录

### HB-04：Kimi CLI

**命令和退出码**

```text
command -v kimi                         # 0
kimi --version                           # 0; 2.2.0
kimi --help                              # 0
```

路径为 `/Users/mnzero/.kimi-code/bin/kimi`。帮助显示 `acp [options]`，以及 `session`、`doctor`、`login` 等命令；帮助还指向 `https://moonshotai.github.io/kimi-code/`。该 URL 是 CLI 自带的官方文档引用，本文没有联网抓取或调用。

本次没有运行 `kimi acp`，没有进行 ACP initialize/能力协商，没有建立会话，没有传 prompt，没有调用工具，也没有测试停止、崩溃清理、凭证隔离、网络禁用或 VM 内路径。因此 HB-04 的正式结果必须保持 **unknown/blocked**，直到真实 VM 中取得协议逐帧记录、工具调用记录和停止/重启证据。

### HB-05：Pi RPC

**命令和退出码**

```text
command -v pi                         # 0
realpath "$(command -v pi)"           # /Users/mnzero/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js
pi --version                           # 0; 1.3.0
pi --help                              # 0
pi rpc --help                          # 0; 回退到总帮助，非专用 rpc 子命令
pi --help-rpc                          # 1; Error: Unknown option
```

已安装包的本地资料来源：`/Users/mnzero/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/package.json`（版本 `1.3.0`）和同包 `docs/rpc.md`。文档声明 RPC 是长驻子进程，stdin/stdout 使用严格 JSONL；记录族包括 stdin command、stdout `response` 和 session events；文档列出 `prompt`、`abort`、`abort_bash`、`get_state`、session 管理等入口，并明确关闭 stdin 请求有序退出。该文档和帮助是接口资料引用，不是运行通过证据。

**隔离启动烟测**

```text
PI_OFFLINE=1 \
PI_CODING_AGENT_DIR=/tmp/hb-rpc.evbKBh/pi-config \
PI_CODING_AGENT_SESSION_DIR=/tmp/hb-rpc.evbKBh/sessions \
pi --mode rpc --no-session --no-mcp --no-skills --no-extensions </dev/null
# 退出 0；stdout/stderr 为空；仅临时 config 下出现 models-store.json、auth.json
```

这里 stdin 立即 EOF，故没有发送 JSONL command，也没有证明 `response` 格式。正式 HB-05 还需在真实 VM 中用无业务数据的协议握手/固定测试数据记录：启动参数、JSONL framing、`get_state`、prompt 接受与 settled 事件、工具调用、`abort`/`abort_bash`、关闭 stdin、异常退出和 session-dir 隔离。Pi 文档的参考入口为同包 `docs/rpc.md`，其中链接至 `docs/rpc-commands.md`、`docs/json.md`、`docs/session-format.md` 和上游 `rpc-types.ts`/`rpc-client.ts`；本机文档存在不等于这些行为已经验证。

### HB-06：dsh SDK profile

**命令和退出码**

```text
command -v dsh                        # 1; missing
DUMMY_PROFILE=... dsh --version       # 127; env: dsh: No such file or directory
DUMMY_PROFILE=... dsh --help          # 127; env: dsh: No such file or directory
python3 -c 'import dsh'               # 1; ModuleNotFoundError: No module named 'dsh'
```

没有发现 dsh CLI、Python SDK 或 profile。没有猜测包名、安装来源或协议入口。`config/runtime-candidates.json` 目前只记录 `dsh` 的协议候选为 `SDK JSON-RPC`，source 为 `dsh official release`，但没有版本、哈希、许可证、命令或 evidence；candidate command `dsh-probe` 仍为 `to-define`。

HB-06 后续必须提供官方发布来源/commit、制品哈希和许可证，明确 SDK profile 文件格式及其无密钥隔离位置，并在真实 VM 中记录最小无业务数据的版本/帮助、profile 解析、JSON-RPC request/response、会话创建/恢复、停止/取消、错误和清理。仅发现 profile 文件或启动 SDK 不能标记 pass。

### HB-07：本地模型候选

**命令和退出码**

```text
command -v ollama lmstudio llama llama-cli mlx-lm qwen  # 每项 1; missing
ollama --version                                       # 127
ollama list                                            # 127
python3 -c 'import mlx'                                # 1; ModuleNotFoundError
```

按只读方式检查了用户目录的浅层候选名称；没有发现可作为本地模型制品或运行时的候选。未读取或打印密钥，也没有启动模型服务。因没有模型 ID、量化、runtime、artifact 路径/哈希、来源或许可证，HB-07 保持 **blocked**。`config/runtime-candidates.json` 的 local model 项仍为 `id/quantization/runtime/source hash/license = null`、状态 `to-probe`，与本次观察一致。

HB-07 需要在授权真实设备/VM中选择并记录模型制品、格式和哈希，确认加载 runtime 与资源上限，使用固定合成输入完成最小推理和工具调用/拒绝调用探针，记录停止/超时/重启、隔离和许可证证据。模型服务进程能启动本身不能产生 pass。

## 与当前 manifest/candidate 的核对

`config/runtime-candidates.json` 的 manifest 状态为 `candidate-lock`，purpose 明确是 HB-01 feasibility candidates；`probeRecord.rule` 明确规定组合只有在所有组件有真实 probe evidence 后才可脱离 `untested`，文档或进程启动不能产生 pass。本次没有修改该 manifest，也没有改变其 `to-probe`、`untested` 或 null 字段。

与本次相关的当前记录如下：

| manifest 记录 | 当前值 | 本次处理 |
|---|---|---|
| `harnesses.kimi-cli` | version null，protocol ACP，status `to-probe` | 保持；本报告只补充本机发现 2.2.0，不回写候选锁 |
| `harnesses.pi` | version null，protocol JSONL RPC，status `to-probe` | 保持；本报告只补充本机发现 1.3.0 |
| `harnesses.dsh` | version null，protocol SDK JSON-RPC，status `to-probe` | 保持；CLI/SDK 缺失 |
| `models.local[0]` | id/quantization/runtime 均 null，status `to-probe` | 保持；未发现本地运行时/模型 |
| 四个 combinations | status `untested`，evidence null | 全部保持 `untested` |
| `candidateCommands.kimi-version/pi-version` | status `not-run` | 本次执行，但未修改 manifest；正式集成应由负责人决定是否单独回写版本发现记录 |
| `candidateCommands.dsh-probe/local-model-probe` | command null，status `to-define` | 保持；不能凭空定义官方入口 |

HB-01、HB-02、HB-03 正式文档和实现未修改。本次只新增本报告。

## 官方接口文档引用核对

- Kimi CLI：本机 `kimi --help` 的 Documentation 行引用 `https://moonshotai.github.io/kimi-code/`，并列出 `acp` 入口；本次未联网验证文档内容，也未把帮助当作 ACP 通过。
- Pi：已安装官方包内 `docs/rpc.md` 是本机可复核的 RPC 资料，描述 JSONL framing、response/event、session 和 shutdown；文档引用了 `rpc-commands.md`、`json.md`、`session-format.md` 及上游 `rpc-types.ts`/`rpc-client.ts`。这些是接口资料来源，实际兼容仍须逐条探针。
- dsh：manifest 只有 “dsh official release” 的泛化来源，没有可核对 URL、commit 或 SDK 文档入口；状态必须是 blocked/unknown，直到 HB-06 锁定官方来源。
- 本地模型：manifest 只有 HB-07 选择模型的占位记录，没有具体制品来源；不能把搜索结果、目录名或服务启动当作来源/兼容证据。

## 交给 HB-04..07 的真实 VM 补证清单

1. 固定 VM 镜像、架构、OS、网络策略、资源限制、工具/模型制品来源、SHA-256、许可证和环境变量白名单。
2. HB-04：Kimi ACP initialize、能力/版本协商、最小合成 prompt、工具调用与拒绝、session 恢复、取消/停止、异常退出和无密钥隔离。
3. HB-05：Pi RPC 严格 JSONL framing、request ID 关联、`get_state`、prompt 生命周期直到 `agent_settled`、`abort`/`abort_bash`、stdin EOF、session-dir、stderr 与 stdout 分离和崩溃清理。
4. HB-06：dsh 官方 SDK/profile 版本与 schema、profile 隔离、最小 SDK JSON-RPC、会话/停止/错误行为、依赖哈希和许可证；若没有真实 SDK，维持 blocked。
5. HB-07：本地模型 artifact ID/格式/量化/runtime、哈希/来源/许可证、加载资源、固定合成推理、工具调用边界、停止/超时/重启和隔离；没有制品证据就维持 blocked。
6. 每个组合必须分别跑 CLI 和 MCP/tool entry point（如任务要求），保存原始协议记录及退出码；任何一项缺证据都不能把组合从 `untested` 改为 pass。

## 本次未做事项

未联网发送业务数据；未安装未知依赖；未提交或读取密钥；未实现 Kimi/Pi/dsh/模型适配器；未修改 `config/runtime-candidates.json`、HB-01、HB-02 或 HB-03 正式文件；未启动真实 VM、远端 harness、模型服务或业务流程。
