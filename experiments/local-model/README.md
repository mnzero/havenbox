# HB-07 本地模型候选探针

`probe.py` 和 `run.sh` 仅检查本机已存在的运行时与模型制品，不下载、安装或访问云端。探针读取模型旁的 `<model>.<suffix>.json` 元数据，要求精确 `modelId`、量化、模板、许可证和 runtime；文件名不能代替模型身份。

运行示例：

```sh
experiments/local-model/run.sh \
  --candidate-id local-llama-candidate \
  --runtime llama-server \
  --model /absolute/path/model.gguf \
  --license Apache-2.0 \
  --protocol local-http \
  --tool-entry-point mcp \
  --tool-schema /absolute/path/tool-schema.json \
  --offline \
  --output /tmp/hb07-record.json
```

只有在调用方已经通过独立控制证明物理断网时，才设置 `HB07_NETWORK_DISABLED=1`；该值与 `--offline` 只让 `offlineDeclaration` 前置声明检查通过，不证明实际离线运行。`--min-memory-gib` 可设置资源下限；GPU 仅记录系统探测结果，未被探针自动视为可用。工具 schema 未配置时为 `unknown`，指定的文件不存在时为 `blocked`，JSON 或结构不一致时为 `fail`。运行时不存在、模型损坏或缺失、元数据缺字段、许可证缺失或资源不足会输出 `blocked`、`unknown` 或 `fail`；不存在云端回退路径。无用途的 `--source` 已移除，传入会被参数解析器拒绝；来源只读取元数据的 `sourceUrlOrCommit`。

## 证据记录约定

输出 JSON 直接作为 `doc/verification/HB-07.md` 中的前置检查证据。输出始终包含 `scope=preflight-only`、`runtimeValidation=unverified`；`preflightResult`、`actual.preflightCombination` 和 `actual.toolSchema` 可以通过，但不代表模型验收通过。真实模型加载、GPU 执行、离线运行、工具调用和组合验证均未执行，对应 `actual.modelLoading`、`gpuExecution`、`offline`、`toolCalling`、`combination` 始终为 `unknown`。总结果保留前置检查的 `fail`/`blocked`，否则为 `unknown`；默认及前置检查全通过时均返回退出码 2。

`sha256` 对本地模型文件逐块计算；运行时版本来自实际版本命令；命令、平台、资源快照、离线声明、工具入口和未验证边界均保留。GGUF magic 和完整元数据不能证明模型有效，探针不能证明模型加载或工具实际执行，必须另行在真实制品和受控 VM 中执行验证。回归检查：`python3 -m unittest discover -s experiments/local-model -p 'test_*.py' -v`。
