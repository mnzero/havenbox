# HB-13 CSV engine (candidate)

纯 Rust 标准库；无第三方依赖、网络、shell、SQL/eval。`Engine` 是每任务的受控对象：可信 supervisor 提供已验证输入字节，返回不透明 `FileId`；五工具只接受本实例 ID 和 `Operation` / `Condition` 类型，不接受路径。ID 是实例内引用，不能跨实例或持久化复用；未来适配器须把会话文件版本 ID 映射至该 registry，不能直接反序列化内部索引。`schema.json` 是待适配器使用的声明式 JSON 合约，本库不是 JSON/MCP/CLI server。

- UTF-8（显式报告 BOM），严格逗号 CSV，唯一非空表头，支持 CRLF/LF、引号换行和双引号转义；字段数/格式错误拒绝整个输入。
- 金额 scale=2，16 位整数，语法对应 manifest；无浮点、trim、指数、正号、隐式舍入。负零格式化为 `0.00`，每次累计检查范围（中间溢出也拒绝）。数值校验发生在指定数值列操作时，inspect 仅提供候选类型。
- concat 要求完整表头顺序相同；filter 的文本比较为原始 UTF-8，decimal 比较严格解析全部行。重复键采用字符串元组，空串参与，两种保留方式稳定。aggregate 按键元组字典序输出，计数是参与行数，不是输出组数。空输入无分组产生 count=0/sum=0；有分组时产生零组。
- `Fact` 记录实际操作、来源输入 ID、输入/输出/排除/解析失败行数、确定性金额和成果引用。失败仅返回诊断而不登记事实或成果。`write_report` 只允许已有 `FactId`，固定模板，不接收自由 Markdown 或模型数字。来源为实例内不可变输入对象；外部内容摘要/文件版本绑定由未来文件通道负责。
- 默认 20 文件、50 MiB/文件、200 MiB 总字节、100万行、256列、1 MiB/单元格、512 MiB 保守内存预算、64 MiB 聚合工作预算、10万组、200预览行/2 MiB预览、50 MiB序列化输出。限额是拒绝上限，不承诺所有上限组合同时可用；解析前按字节128倍预留保守对象/峰值开销，因此某些合法大文件会先报 memory_limit。
- 超过聚合工作预算时使用 `Scratch::trusted_directory` 能力（仅可信调用方可配置）。分组和重复键使用定长计数/金额的磁盘外部映射，一次只读取一个键；磁盘总预算在追加前检查，更新不增加空间，单键内存超限拒绝。实现是 O(行数×组数) 的保守线性扫描，不声称大数据吞吐优化。输出表仍受全局内存预算，不能无限产生输出。系统 allocator/RSS 的硬上限必须由来宾 worker/cgroup 强制，不是本库的估算器所能认证。
- 临时文件 create_new 后立即 unlink，仅保留打开句柄；返回、IO错误、取消和 unwinding 都关闭句柄，无命名部分成果，不删除无关文件。调用方须保证目录及祖先由可信进程独占、不可被替换（本库不提供 HB11 的 namespace 认证）。磁盘额度由可信调用方指定，应不超过 manifest workerScratchBytes；当前单引擎串行至多一个临时文件。
- `cancellation_handle` 可由可信 watchdog 置位；每行及磁盘扫描记录检查，取消不发布。解析/序列化有界但未提供细粒度取消；真正进程终止与超时属于 supervisor。

运行：

```sh
SDKROOT=/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk cargo test --manifest-path guest/Cargo.toml --locked --offline
python3 scripts/engineering.py check
```

测试读取原 HB-01 CSV，断言固定 expected-results 中的所有业务期望；原 Python 检查器继续独立校验期望文件及输入哈希，不代替 Rust 功能测试。HB12导入/导出、HB14计划许可、MCP/CLI、Linux VM、断电恢复与实际硬资源隔离未在本任务实现或验证。
