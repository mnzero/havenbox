# MVP-02 文件边界与真实 VM 任务通道验证

日期：2026-10-25
结论：**完成（candidate-25 通过 MVP-02 验收）**

本记录依据 PRD 第 4、6、9、13 节和 `doc/development-tasks.md` 的 MVP-02
验收，汇总同一 candidate-25 冻结组合的构建、真实 QEMU/HVF 探针和当前
工程门禁。真实证据只取自保存的完整日志、REPORT 和 receipt；Swift、Rust、
Python 单元测试用于代码回归和边界负例，不替代真实 VM 证据。

## 范围与结论边界

MVP-02 交付宿主导入和分块传输、来宾只读 staging、成果提交与 ACK、租约、
任务 cgroup、停止、失联、强杀和重启核查。`hb-supervisor --run-files` 仍是固定
文件通道探针：它把收到的 CSV 原样返回以验证跨边界字节、哈希和生命周期，
不调用 CSV CLI，不接入模型、MCP、外部 harness 或任意命令。真实 CSV CLI
成果链属于下一阶段 MVP-03。

宿主 `HostFileBoundary` 以 `O_NOFOLLOW` 打开导入文件，绑定
device/inode/size/mtime 后分块复制到私有 workspace，并在发布前复核并发替换；
保存的输入与成果为只读。来宾以类型化 task/file/version ID 和 epoch/request
绑定操作，严格校验分块顺序、累计 SHA-256、媒体类型和配额，原子发布 `0444`
输入；artifact 从已绑定 fd 校验并拒绝多硬链接源。artifact raw chunk 上限为
190 KiB，保证 JSON/Base64 包装后不超过 262144-byte frame 上限。

路径穿越、目录、符号链接、特殊文件、并发替换、超限输入、错误哈希、非法
媒体类型、乱序块和 artifact 路径/硬链接等负例由实际 Swift/Rust 回归覆盖；
没有把这些非 VM 测试写成真实 QEMU/HVF 观察。真实 VM 矩阵专门证明下列
通道、清理和生命周期行为。

## 冻结候选与运行时

- Candidate：`experiments/vm/assets/hb11-candidate-25`
- 构建 receipt：
  `experiments/vm/assets/hb11-candidate-25/candidate25-build-receipt.json`
  （SHA-256
  `3902933e34314fc920488dbe588cfdf84b1dacc201c52cea87478c41fb0ef751`）
- 构建报告：`experiments/vm/state/hb11-candidate25-build/REPORT.md`
- 运行时：UTM 5.0.8 的 QEMU 10.0.12，HVF；每次真实门禁前均由
  `experiments/vm/replay-hb02-local.sh verify` 和独立 launcher `--version`
  核对。
- 来宾：Alpine 3.24.3、kernel 6.18.58-r0、静态 Linux AArch64
  `hb-supervisor`。构建使用 Zig 0.17.0、Rust/Cargo 1.99.1、SDK 26.5、
  e2fsprogs 1.47.4；candidate-25 两次洁净构建的五个发布制品逐字节相同。

| 制品 | Bytes | SHA-256 |
| --- | ---: | --- |
| `Image` | 10412544 | `849394914e6a93cc9bb17e901f445ce21fcd7e4528541d9496a74d6a6ccee319` |
| `initramfs-hb11` | 1500746 | `6ac15f1b40b8f1e25e4fa336e8b5d9386dd53e90956d6c6e47d86e7c3a73ddd0` |
| `root.raw` | 268435456 | `2f0611c06b0ccf140ad542f5644349c50c7145f50d60afa15598560eea3199c0` |
| `data.raw` | 268435456 | `c81101c84702c629244ca3bb9015618bdf0e47530abc774fea5e564520e249fd` |
| `manifest.json` | 1072 | `7372e2c1bfd689e3ce60beca23605a9c412b5dc922efd6fe7117b57c8ca0a5cd` |
| `hb-supervisor` | 772848 | `0161a406a97090b45d133fa8d808945fdfb406bd57b256defd57df925dc60bbe` |

Supervisor 经 `file`/`llvm-readelf` 验证为 AArch64、静态链接、stripped，
无 `INTERP`/`DYNAMIC`，`GNU_STACK` 不可执行。candidate 发布前后均核对 receipt、
manifest reverse keys、只读模式、initramfs `/init`、root `hb-init` 和嵌入的
supervisor；所有真实运行使用独立 `0700` 短 HOME 与独立 `0600` data clone。

## 真实 QEMU/HVF 验收矩阵

| 验收项 | 真实命令/证据 | 必要观察 | 结果 |
| --- | --- | --- | --- |
| HB-11 harness normal | `HOME=/tmp/hb25-harness-normal.EsfBCi python3 experiments/vm/probe-hb11-channel.py experiments/vm/assets/hb11-candidate-25 /tmp/hb25-harness-normal.EsfBCi/data.raw harness normal`；`experiments/vm/state/mvp02-candidate25-regression-gates/hb11-harness-normal/` | 各 1 个 `HelloAck`、`SyntheticTaskCompleted`、`Stopped`；无残留 | **通过，exit 0** |
| normal | `HOME=/tmp/hb25-mvp02-normal.QKAnxI experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 normal`；`experiments/vm/state/mvp02-candidate25-regression-gates/mvp02-normal/` | `roundtrip=true input-readonly=true bytes=252 sha256=659af3f0aa6532a5f24476193cf79a6c590ba8df13981b4b404fc73469cf4f46`，artifact announce/read/hash/ACK，`stop-ack=true task-processes=0`，fail-closed，0 个 `HB_FILE_ERROR` | **通过，exit 0** |
| disconnect | `HOME=/tmp/hb25-disconnect.2pjbUN experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 disconnect`；`experiments/vm/state/mvp02-candidate25-regression-gates/disconnect/` | `host-disconnect-channel-closed=true`，fail-closed，`restart-replay=false task-processes=0` | **通过，exit 0** |
| lease-expiry | `env HOME=/tmp/hb25l.bbi3rv experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 lease-expiry`；`experiments/vm/state/mvp02-candidate25-lease-rerun-short/` | 2 个 fail-closed、完整 `MVP02_SUPERVISOR_EXIT status=78`、`supervisor-exit-status=78`、`lease-expiry-cleanup=true`、重启不重放 | **通过，exit 0** |
| protocol-error | `env HOME=/tmp/hb25f.4vdrqW experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 protocol-error`；`experiments/vm/state/mvp02-candidate25-final-six/protocol-error/` | `protocol-error-cleanup=true`、2 个 fail-closed、重启不重放、0 个 `HB_FILE_ERROR` | **通过，exit 0** |
| ack-mismatch | `env HOME=/tmp/hb25f.MM5hTa experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 ack-mismatch`；`experiments/vm/state/mvp02-candidate25-final-six/ack-mismatch/` | `ack-mismatch-rejected=true cleanup=true`、重启不重放、无残留 | **通过，exit 0** |
| ack-replay | `env HOME=/tmp/hb25f.4xKdAy experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 ack-replay`；`experiments/vm/state/mvp02-candidate25-final-six/ack-replay/` | `ack-replay-rejected=true cleanup=true`、重启不重放、无残留 | **通过，exit 0** |
| unacked-stop | `env HOME=/tmp/hb25f.APz4EH experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 unacked-stop`；`experiments/vm/state/mvp02-candidate25-final-six/unacked-stop/` | `unacked-stop-rejected=true cleanup=true`、重启不重放、无残留 | **通过，exit 0** |
| chunk-boundary | `env HOME=/tmp/hb25c.llKyWu experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 chunk-boundary`；`experiments/vm/state/mvp02-candidate25-chunk-boundary-final/` | 389120-byte 精确往返、只读输入、`artifact-multichunk=true exact-boundary=true eof=true`、Stop、fail-closed；0 个 `HB_FILE_ERROR`/panic | **通过，probe/corrected validator/residual 均 exit 0** |
| kill | `env HOME=/tmp/hb25r2.f2luxP experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 kill`；`experiments/vm/state/mvp02-candidate25-kill-restart-rerun-2/kill/` | `qemu-killed=true ... residual=false`、fail-closed、重启不重放；进程/job/socket/state 零残留 | **通过，exit 0** |
| restart | `env HOME=/tmp/hb25r2.2UM6iP experiments/vm/run-mvp02-files.sh experiments/vm/assets/hb11-candidate-25 restart`；`experiments/vm/state/mvp02-candidate25-kill-restart-rerun-2/restart/` | 252-byte 往返、Stop ACK、2 个 fail-closed、`restart-replay=false task-processes=0`；零残留 | **通过，exit 0** |

每个证据目录的 `command.txt` 保留完整 argv/HOME，`run.log` 保留完整串口输出，
`receipt.json` 或 REPORT 记录退出码、
marker、clone hash 和前后残留比较。真实运行结束后相关 QEMU/helper/supervisor
进程与 Havenbox launchd job 均回到空 baseline，task-owned socket 和 QEMU state
目录均为空；candidate 哈希和元数据与 preflight 一致。

## 失败历史与纠正

- `mvp02-candidate25-regression-gates` 的首次 lease 探针本身 exit 0，但完整
  `MVP02_SUPERVISOR_EXIT status=78` 串口行被终止输出截断，严格门禁 exit 1；
  未从清理 marker 推断为通过。随后只重跑 lease，短 HOME 避免 QEMU 104-byte
  Unix socket 限制，完整 marker 和 PID1 存活观察均通过。
- `mvp02-candidate25-final-six` 的首次 kill 探针 exit 0，但 SIGKILL 后留下三
  个 socket filesystem node，残留门禁 exit 1，restart 按首败停止未运行。
  host probe 后续只修复 socket readiness/close 行为；`kill-restart-rerun-2`
  按顺序复验 kill、restart，二者 probe/marker/residual 均 exit 0。
- candidate-25 最初没有保存直接的 chunk-boundary 证据，不能沿用 candidate-24
  或从 `final-six` 的文字声明推断。本次补跑一次真实 candidate-25 gate。
  原 evidence wrapper 额外要求该单模式输出 `restart-replay=false`，因而留下
  `marker-validation.exit-code=1`；该模式契约不包含第二次启动，重启不重放由
  独立 restart gate 证明。原始日志和误判记录未改写，新增 corrected
  mode-specific validator exit 0，未修改 probe，也未再次运行 VM。

## 工程门禁

最终 source snapshot 由 `contracts.baseline.source_snapshot()` 的权威路径列表
生成，纳入 `guest/image/deterministic-newc.py` 和
`guest/supervisor/src/aarch64_linux_open_flags.rs`；SHA-256 为
`ee0e108b7fc609ba237fcec96202f9c768c9b091aecb9024fb276cd98e77e73a`。

| 命令 | 结果 |
| --- | --- |
| `python3 scripts/engineering.py check` | 首次：Swift/Rust build 通过后因 source snapshot path drift exit 1；按权威输出刷新 manifest 后复验 exit 0，完成 Swift 实编/回归、23 个 contract、CSV/计划检查及 Cargo 全 workspace 测试 |
| `cargo fmt --manifest-path guest/Cargo.toml --all -- --check` | exit 0 |
| `python3 -m unittest discover -s tests -p 'test_*.py' -v` | exit 0，44 tests |
| `python3 -m py_compile experiments/vm/*.py tests/*.py tests/contracts/*.py scripts/*.py guest/image/*.py` | exit 0 |
| 对 `experiments/`、`guest/`、`scripts/`、`tests/` 下全部 `*.sh` 执行 `bash -n` | exit 0 |
| `git diff --check` | exit 0 |

## 完成判定与下一阶段

candidate-25 的构建身份、HB-11、文件往返/只读/哈希/分块边界、协议与 ACK
拒绝、停止/失联/租约/强杀清理、重启不重放以及最终工程门禁均有直接证据；
MVP-02 验收项全部通过，因此本任务状态为 **已完成**。这不赋予发布资格，
也不证明模型、真实 CSV CLI、完整安全验收或内部包通过。

下一阶段入口是 MVP-03：把固定 Linux guest CSV executable 和 CLI 适配器接入
当前 staging、CallPermit、Supervisor 与成果对象存储，并在同一冻结组合中真实
验证 inspect、concat、filter、aggregate、report 及许可/参数/成果篡改负例。
