# HB-08 内部协议与领域契约 v1（candidate）

这是正式实现的输入，不是已运行的网关或沙箱。`config/schemas/domain.schema.json` 定义可序列化 DTO；其中审批 DTO 只是记录，不能凭反序列化构造有效权限。宿主独占批准记录、撤销表、epoch、单活动槽、预算及证据注册表。UI 与来宾均不能提供 `build_flavor` 或可信注册表。

## 版本、证据和依赖

- `candidate`：待实现/待探测；`probe`：仅有明确范围的真实探针；`validated`：同一 binding 下 MVP-07 安全与故障验收通过；`frozen`：版本/制品/许可证与 MVP-08 内部包审批齐全。枚举值本身不是授权。
- MVP-01 冻结唯一候选组合不等于产品可执行。普通执行资格要求 frozen manifest、validated/frozen 组合以及同一 binding 的 MVP-07、MVP-08 两个可信 receipt；receipt 必须分别标明任务 `MVP-07`、`MVP-08`，任意缺项、旧 HB receipt 或开发 ValidationRunGrant 均拒绝。资格也不替代运行时授权。
- `binding = SHA256(hb-canonical-json-v1({manifest, combination_without_state_and_evidence}))`。规范 JSON 为 UTF-8、键排序、无空白、禁止 NaN/重复键；哈希版本是本契约而非声称 RFC8785。正式跨语言实现必须用共享 golden vectors 校验，当前 Python 是参考实现。
- manifest 包含 MVP 构建依赖、资产、协议/schema/检查器文件 SHA-256；已有资产保留实测 hash，缺失制品保持 null 并列入 unresolved。任一版本、端点/凭据 revision、模型/模板、参数 schema、工具策略、资源上限或接口内容变化，必须生成新 revision，重算清单并重跑；旧 binding 不再接受。证据不能自动“迁移”。不提供自动重签或自动授予资格入口。
- 外部 receipt 注册表仅由宿主从受审证据导入；JSON 中的路径、`pass` 或 digest 不具有签名效力。HB-10、HB-11、HB-13、HB-14、HB-16 只是候选输入，实际发布信任根与最终结论只由 MVP-07、MVP-08 建立。

## 宿主/来宾帧 `hb.channel.v1`

候选二进制头：magic `HBX1` (4 bytes)，version u16=1，channel u8（1 control / 2 file / 3 inference），flags u8=0，epoch u64，request UUID 16 bytes，payloadLength u32，payload SHA-256 32 bytes；共 68 bytes，整数大端。先检查长度/版本/通道/epoch，再分配载荷；校验和不是认证。

载荷为严格 UTF-8 JSON，对应 domain schema。启动握手必须绑定本次随机实例和 epoch；握手/租约失效先阻断新请求，随后停止活动 cgroup。具体 transport、租约调度和背压由 HB-11 实现，当前不声称 HB-02 pipe 测试等于此帧已集成。

| 通道 | 上限 | 宿主→来宾 | 来宾→宿主 |
|---|---:|---|---|
| control | 64 KiB | Hello / Stop / CallPermit | HelloAck / Event / Stopped |
| file | 每块 256 KiB（含 JSON 编码后字节） | ReadArtifact / FileChunk | FileChunk，仅响应宿主已登记请求 |
| inference | 4 MiB | InferenceResponse / Cancel | InferenceRequest，仅固定路由引用 |

未知版本、方向、类型、跨 epoch、重复/未知 request ID、过长和坏哈希一律拒绝，不能把未知事件映射成功。停止控制使用独立设备和有界队列，不与文件或推理传输争同一个阻塞读取。宿主 socket 目录 0700，QMP 不开放给来宾。FileChunk 不含宿主路径；导出必须关联宿主先登记的 ReadArtifact。

HB-11 candidate 在 Swift/Rust 都实现了固定 68-byte header、先验长度、payload SHA-256、严格 UTF-8 JSON、重复 JSON key、消息类型/方向和 request registry 校验；Rust `FrameReader` 先读固定头并按通道上限分配，三通道使用独立有界队列。`Hello`/`HelloAck` 的 JSON `instance` 必须匹配宿主生成实例，heartbeat 的 `sequence` 为严格递增十进制字符串，epoch 同时受 header 与运行参数绑定。SHA-256 只做传输完整性检查，不是认证；宿主和来宾身份来自固定本地 transport、受管启动实例与 epoch。当前代码已通过 host-native 单元测试，但正式 Linux image、virtio-serial 流和 QEMU 强杀尚未在本工作树实测，不能据此把 candidate 提升为 validated。

## 领域边界

SessionId、TurnId、TaskId、CallId、FileVersionId 采用带前缀字符串的独立 wire 类型；Swift/Rust 包装类型已由 HB-09 实现前缀与 ASCII 后缀校验，各种 ID 不能混用；尚未实现领域 DTO 序列化。SessionBinding = Agent(recipeRevision) | ToolsOnly(toolPolicyRevision)。Turn 与 ExecutionTask 分开；首次披露只需 turn，批准计划之后才创建 task。ModelRoute 为互斥 Local/Online，真实凭据永不在 DTO 中。

`CallPermit` 必须宿主批准且绑定 call/task/plan/arguments/step/epoch/期限；重复 call ID 只读核查记录，不重做写操作。模型/工具仅返回建议，`ToolSuggestion` 永不等于许可。ArtifactVersion 绑定内容 hash、provenance 与 completeness，成功退出码不代表成果完整。

MVP 运行路径只使用 `BuiltinAgent` 和固定 CSV CLI，不接受外部 HarnessAdapter、MCP 或任意 executable。模型只返回澄清问题或声明式计划，`ToolSuggestion` 永不等于许可；宿主仍须拒绝未知工具、字段、表达式和宿主路径。旧 ACP、Pi RPC、dsh SDK 适配代码仅作历史候选保留。

恢复策略为单工作空间、单活动任务、只读历史；启动时 epoch 更新，旧 grant/permit 失效，未完成任务标记 `Interrupted`，不自动重放工具或联网请求，也不继续对话。原生恢复与多会话不属于 MVP。未知结果先核查，清理失败占用活动槽。

## ValidationRunGrant（合成测试启动资格）

`contracts/baseline.py:authorize_validation` 是纯校验函数，不启动进程或请求，也不签发 token。可信宿主通过随机不透明 ID 注册 exact grant，内部构建才能调用；普通/release 构建拒绝，即使持有合法 grant。不可用环境变量或来宾字段改变构建类别。MVP-08 必须证明 release target 排除测试签发入口。

grant 绑定候选 binding、组合、受审合成数据集 digest、完整路由 digest、epoch/session/turn、显式测试授权 digest、notBefore/expiresAt（最长 1 小时）和 requests/inputTokens/outputTokens/bytes/toolCalls/seconds 限额。请求 reservation 表示本次请求后的累计最坏预留总量，不是单次增量；宿主必须在单一事务中核查并预留，不能信任来宾自报计数。未知用量保留预留，撤销即删/失效注册记录；重启 epoch 变化。测试数据集注册表应以实际字节和固定期望形成 digest，不接受请求自报 synthetic=true。

它**仅替代开发验证的“已有组合证据”前置**；文件授权、首次联网披露、CallPermit、TLS/地址限制、无网卡、工具沙箱、配额预留、停止与进程核查全部仍须正常执行。路由未知或必需版本未固定时拒绝 grant；当前唯一候选仍有未固定制品，不能直接签发可执行 grant。ValidationRunGrant 永远不能作为 MVP-07/MVP-08 receipt 或发布资格。

## 内置 Agent 与推理参数校验

兼容矩阵只接受 `builtin-kimi-k3-1-pro-csv`：内置 Agent、Moonshot AI Kimi API `kimi-k3.1-pro`、OpenAI 兼容 Chat Completions、固定 HTTPS `/v1/chat/completions` 端点和 CLI 工具入口。第二组合、外部 harness、本地模型、MCP、多会话恢复、其他模型或非 HTTPS 端点均由 schema 拒绝；接受唯一候选标识不代表组合已可执行。

`contextTokens` 和 `maxOutputTokens` 是本地必需预算规则，必须 `supported=true`，默认值及 minimum/maximum 均为正整数，valueSchema.type 固定为 integer；8192/2048 是 Havenbox 上限，不声称是提供方上下文上限。`maxOutputTokens` 映射官方 `max_completion_tokens`，`reasoningEffort` 映射 `reasoning_effort` 并固定为 `low`（可选 `high`/`max`）。请求使用 SSE 与 usage、`response_format=json_schema` 和 strict function tools；temperature/top_p/penalty 不发送。`check_schema` 验证默认值边界，`normalize_parameters` 拒绝未知或不支持参数，并确保输入、模板、工具及最大输出总和不超过本地上下文预算。

## CSV 与资源候选

金额语法采用 manifest 正则的全串匹配，最多 16 位整数、2 位小数，解析成 scale=2 的有符号整数；禁止指数、空白、NaN、Infinity、前导加号与隐式舍入，超过 18 位 scaled 值或累计溢出即拒绝发布部分成果。字符串按原始 UTF-8 精确比较；空字符串参与重复键，缺列错误；稳定输入顺序，两种重复保留规则显式选定。这是 HB-13 引擎契约，不是本次已经实现的 CSV 引擎。

资源沿用技术方案 §8.2；新增 worker 内存 512 MiB、scratch 2 GiB、聚合组 100000 为保守候选，非实测容量。所有计数包含解析/编码后的实际字节；CSV/预览/工具/VM/请求的各项具体上限见 manifest。超限失败不截断计算、不自动扩容/换模型；preview 可以明确标记裁剪但不能影响计算。适用上限取配置/模型/工作空间/运行时最严值。
