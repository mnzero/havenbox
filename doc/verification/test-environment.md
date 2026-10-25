# 主链测试环境验证（2026-10-25）

本记录不修改 HB 验收状态。审查基线为 `d675089`（含 HB16），开始时工作树干净；没有提交、推送、下载工具或更改用户 Keychain。

## 公共入口

```sh
python3 scripts/test-environment.py --report-dir .build/test-environment/run-N
python3 scripts/test-environment.py --host-process-tests --report-dir .build/test-environment/run-N-host
python3 scripts/test-environment.py --loopback --report-dir .build/test-environment/run-N-loopback
```

报告目录必须不存在；目录内 `environment.json` 保存版本、解释器路径、隔离环境和 Seatbelt profile，`results.json` 保存逐命令 argv/cwd/耗时/原始退出码/分类及清理结果，独立 `.log` 保留完整输出。engineering 日志继续记录每条 swiftc/clang/cargo/测试二进制命令。退出码 1=失败、2=尚有 blocked、0=覆盖项通过；不能忽略 blocked，也不能把宿主 RPC 与模型/VM 验收混淆。控制台显示聚合分类，原始子命令退出码仍保存在 JSON；退出 0 的协议探针或带结构化 blocked 的测试不能自动视为通过。

默认 Seatbelt 拒绝全部网络；Review 复验发现 Seatbelt 的 `localhost` 会放行 en0，并非严格 loopback，故当前 `--loopback` 也保持拒绝全部网络、报告 blocked 且不启动 harness（详见下文最新复验）。工具链从白名单环境启动，不继承 API Key、代理、NODE_OPTIONS、harness 配置；HOME/TMPDIR、Swift/clang module cache、Cargo target 和 Swift 构建目录每次私有且结束删除，Python 不落 pyc，Cargo `--locked --offline`。使用固定 SDK 26.5 而非指向 27.0 的默认 SDK；已有 `engineering.prepare` 精确检查 Swift/Rust/Cargo/Python。版本不匹配不自动修复。工程仍针对 arm64 macOS 14，不能视为 Intel/最低 macOS/SwiftPM 通过。

`--loopback` 在 sandbox 自检之后立即 blocked，和 `--host-process-tests` 合用也不会执行后者；受控 TCP fixture 仅用于独立门禁回归中的地址验证和拒绝测试，不是 harness loopback 可用性证明。

macOS `/bin/ps` 为 setuid，Seatbelt 内 exec 实测 EPERM；因此 VM host 回归、HB03 probe 与 HB03 真实进程回归这三个命令默认不运行并记录 blocked，必须显式 `--host-process-tests` 才在网络 sandbox 外运行。这些代码不发网络请求，但不能声称它们受网络权限隔离。所有外部 harness 探针仍在 Seatbelt 内。HOME 不能隔离真实 macOS Keychain，本入口不写任何 Keychain 项。

## 修复

- `engineering.py` 支持 `HB_BUILD_ROOT` 与 `CARGO_TARGET_DIR`，包括 helper 检查及打包源路径，避免并发运行共享产物。只更新 runtime manifest 中受影响的源码/脚本摘要，资格及验收字段不变。
- HB03 删除按脚本名全局清理历史进程及 watcher 全局扫杀；历史 PID 只观察，watcher 只清理本次记录的组。helper/租约必须自然退出，不能由测试 `reap()` 超时强杀后冒充成功；断言前不再清除被测 child group，finally 负责善后。
- 新增三个真实进程回归：正常场景、故意破坏租约检查必须 TimeoutExpired 且清理、并行同名无关进程不被 watcher 杀死。
- Kimi transport helper 冷启动普通预算由 2s 放宽到 5s，乱序响应辅助进程存活期由 2s 延长到 10s；专门测试写阻塞的 0.3s 不变，超时断言仍保留。

## 实测命令、版本和退出码

最终命令：

```sh
python3 scripts/test-environment.py --host-process-tests --loopback --report-dir .build/test-environment/run-05
# exit 2（blocked，不是失败伪装通过）
git diff --check  # exit 0
```

主动 LSP 检查五个 Python 修改文件均未获得语言服务器回答，不能声称静态类型检查通过；辅助规则提示 HB03 的 ps/CLI 数字解析，现有行为为异常失败而非忽略非法输入。本次验证依据实际运行和 `git diff --check`。

版本：Python 3.15.0；Apple Swift 6.3.3 / swiftlang-6.3.3.1.3；rustc 1.99.1 (58cf8f9c3 2026-10-13) Homebrew；cargo 1.99.1 (5f94df478 2026-08-27) Homebrew；SDK 26.5；Darwin arm64。SQLite 实际输出 3.51.3。

| 检查/实际子命令 | 原始退出码 | 证据范围 |
|---|---:|---|
| `python3 scripts/engineering.py check` | 0 | Swift 实编与 core 31 回归、真实文件 SQLite 迁移/reopen/事务测试、runtime/configuration 回归、真实 Rust CSV 引擎及 supervisor/protocol 单测、HB01 fixture、HB08 baseline、计划校验 |
| `python3 -m unittest discover -s tests/contracts -p 'test_*.py' -v` | 0 | 22 项 |
| 同命令 `-s experiments/harness/kimi` | 0 | 18 项，真实 Kimi 集成 1 项 skip；不可计为该项通过 |
| 同命令 `-s experiments/harness/pi` | 0 | 8 项 |
| 同命令 `-s experiments/harness/dsh` | 0 | 9 项 |
| 同命令 `-s experiments/local-model` | 0 | 4 项前置/负向测试 |
| `python3 -m unittest discover -s experiments/vm -v` | 0 | 5 项 host fake-QEMU 安全回归，仅测试，不是 VM 验收 |
| `python3 experiments/process-lifecycle/probe.py --probe` | 0 | 真实 UI/helper/child 进程组机制，launchd 明确 blocked |
| `python3 -m unittest discover -s experiments/process-lifecycle -v` | 0 | 新增 3 项真实进程测试 |
| `bash experiments/vm/replay-hb02-local.sh verify` | 3 | blocked：当前 worktree 无 tools，assets 只有 guest-payload.sh |
| `python3 experiments/local-model/probe.py --offline` | 2 | blocked：没有受审运行时/模型/许可证 |
| `python3 experiments/harness/pi/probe.py` | 0 | 真实宿主 RPC；结果中 MCP unknown、历史 blocked，未生成模型回复 |
| `python3 experiments/harness/dsh/probe.py` | 3 | blocked：固定 runtime 不可用 |
| `python3 experiments/harness/kimi/protocol_probe.py --local-fixture --output <报告目录>/kimi.json` | 1 | blocked PermissionError；独立只读诊断还发现安装 executable digest mismatch，未绕过 pin |

历史报告保留在 `.build/test-environment/run-01` 至 `run-05`（ignored，不纳入提交）：01 退出1暴露 ps sandbox、源摘要漂移；02 退出1暴露独立脚本摘要漂移；03 退出1暴露 Kimi 2s 冷启动波动；04 退出1其余回归全通过但 Kimi 前置阻塞尚未分类；05 退出2明确保留阻塞。独立 `python3 -B experiments/process-lifecycle/probe.py --probe > /tmp/hb03-25d661.json` 退出0。所有完整子命令与输出以这些目录为准，不把历史失败删除。

## 清理证据与剩余边界

run-05 `scratch_removed=true`、`residual_scratch_processes=[]`、ps 观察退出0。测试结束检查没有本工作树 HB03 child/helper/watcher、fake-qemu、havenbox-helper、qemu-system-aarch64 或 `dev.havenbox.runtime.*` launchd job；grep 无匹配返回1。首次失败曾留下两个本次 HB03 child group 11295/11297，已核对该工作树命令和 PGID 后 SIGKILL 清理；未杀任何历史或他人进程。手动 py_compile 产生的唯一 `experiments/process-lifecycle/__pycache__` 已删除；保留 ignored 报告，不保留编译缓存/数据库。主进程组清理不能证明已脱离会话的任意恶意后代被回收；SIGKILL/断电时 Python finally 也无法执行，不宣称这些边界已验。

真实 VM/HVF/QEMU/launchd：先由操作者将本机已有受审资产放入 runbook 的工作树路径（不要自动下载），运行 [HB02 verify/start/stop](../runbooks/hb02-local-vm-assets.md)，再按 [HB11](../runbooks/hb11-real-runtime.md) 离线编译静态 guest、创建新 candidate、逐次克隆 0600 data、通道/租约/强杀检查与残留检查。公共门当前只探测资产，不自动执行完整 VM 矩阵；这些项目保持 blocked。

Keychain：目前不存在隔离的真实 Keychain 测试入口；需专用测试登录用户或显式临时 keychain 的测试实现（现有 API 默认服务不能靠 HOME 改写），测试唯一 service/account 的 save/read/update/delete 并验证删除，禁止操作用户生产服务；本次未以 TestCredentials 替代其验收。

Kimi/dsh：操作者恢复已审核固定 runtime/hash 和访问权限，不能为当前安装改 pin 来取得绿灯，并解决精确地址 sandbox 过滤阻塞后才能执行真实 loopback 验证（当前重跑 `--loopback` 仍会 blocked）；模型/MCP/guest harness 还需 PRD 授权与受审离线包/网关及真实 VM。没有凭据或批准端点时禁止尝试外部模型。Pi 的宿主协议成功仍不解除这些阻塞。

## Review 修复复验（历史结果，最新见文末）

本轮仅修改 `scripts/test-environment.py`、新增 `tests/test_environment_gate.py` 并更新本文；没有提交、委派或触碰其他已有改动。

- **网络 fail-closed**：真实临时 en0 服务证明 `(remote ip "localhost:*")` 允许访问 `192.168.31.145`；数字地址过滤器则被本机 sandbox-exec 以 65 拒绝（`host must be * or localhost`）。经协调者明确批准，撤掉不安全例外；`--loopback` 先执行真实 sandbox `/usr/bin/true`，随后记录 `loopback-policy.status=blocked`、`reason.code=exact_loopback_filter_unavailable`，退出 2，不启动任何 harness。`network` 始终为 `denied-by-sandbox`，不宣称 loopback 已可用。
- **逐地址校验**：保留 `resolved_loopback`，检查解析结果的每一个地址，拒绝 wildcard、私网、en0、IPv4-mapped IPv6（包括映射的 127 地址）；混合结果中任一非法地址均拒绝。此函数不是内核隔离替代品，当前不用于授予 harness 网络权限。
- **受控 fixture**：回归只在明确的 `127.0.0.1` 和本机 en0 地址创建临时 TCP 服务，先以宿主连接确认服务真实可达，再以真实 sandbox 子进程连接，均得到 `Operation not permitted`。127 fixture 保留作正向控制，但 sandbox 内的例外仍 blocked，绝不退回 localhost 规则。
- **退出码**：sandbox 失败不再在 try 内提前 return；清理后唯一返回聚合结果。`/usr/bin/false` 实进程回归确认子项退出 1、外层进程退出 1、JSON 退出 1，scratch 删除且无残留。
- **Pi 聚合**：读取真实 JSONL 子项；blocked/unknown 令 `pi-real-rpc.status=blocked`，机器可读 `reason.code=pi_incomplete_checks` 携带原子项，同时保留原始进程 `exit_code=0`。日志缺失或非法也 blocked；子项 fail 则 fail。控制台在分类之后打印，不再把部分成功显示为 pass。

最新报告（ignored）：

| 命令/报告 | 实际结果 |
|---|---|
| 默认入口 `.build/test-environment/review-default-final` | exit 2；覆盖回归无 fail，host-process/资产/产品验收 blocked |
| `--host-process-tests` / `review-host-final` | exit 2；真实 HB03 probe 和 3 项真实进程回归、5 项 VM host 回归通过；不是 VM 验收 |
| `--loopback` / `review-loopback-final` | exit 2；只有 sandbox 与 loopback-policy 两项，不启动 harness |
| 两个完整入口内 `engineering.py check` | exit 0，真实 Swift/Rust 构建与测试通过 |
| `python3 -B scripts/check-baseline.py` | exit 0，22 项通过 |
| `python3 -B tests/fixtures/csv/check.py` | exit 0，10 fixture/9 CSV hash 匹配，4 组合仍未测 |
| `python3 -B -m unittest discover -s tests -p test_environment_gate.py -v` | 5 项通过，含真实拒绝/false/loopback阻断 |
| `git diff --check` | exit 0 |

三个 final 报告均 `scratch_removed=true`、`residual_scratch_processes=[]`，ps 观察退出 0；另检查未发现 HB03 child/helper/watcher、fake-QEMU、QEMU、havenbox-helper 或 `dev.havenbox.runtime` launchd job。没有使用 Keychain、模型凭据或下载资产；完整入口仍保留 VM/Keychain/模型等 blocked，Pi MCP/history 子项也不再伪报通过。

保留首轮 `.build/test-environment/review-default` 的 exit 1：Kimi `stdout closed` 子用例曾在 `acp_client.close` 报 `process group cleanup denied`；同轮 host 与 final 两入口均未重现。这是范围外的已观察波动，未修改 Kimi 实现、未删除失败证据，也不因复跑通过宣称已修复。Python 主动 LSP 若无响应不计为静态检查通过，以实测回归为依据。

## dsh 时序修复复验（历史结果，真实 VM 见文末）

仅修改 dsh 测试、公共测试门分类、门禁回归及 README/本文，保留开始时所有已有改动；未修改业务实现、运行时 pin 或提交 Git。原测试从 exec 前起算 2 秒，把新 executable 的冷启动与 fork 调度算进执行 deadline；本轮单项修复前运行通过，未在本轮复现历史偶发失败，根因判断来自原时序及此前观察。

测试现在先启动真实独立进程组，最多 15 秒等待后代 `child-ready`，然后发送 `start` 握手，再进入原有 2 秒 RPC deadline；只替换 Popen 返回为已启动的真实进程，pipe I/O、timeout 和 killpg 均仍真实执行。后代收到 start 后才启动存活 marker 计时，避免启动等待污染断言。exec 失败、launcher 提前退出或握手超时输出 `HB_TEST_BLOCKED` JSON，unittest 标记 skip；公共门解析后记录 `dsh-regression.status=blocked` 和 `reason.code=regression_startup_blocked`，保留原始 exit 0 但聚合退出 2，绝不当作 pass。finally 始终清理本次组、wait 回收及关闭 pipes，不清理历史进程。增加 exec 失败结构化输出和门禁 blocked/fail 优先级回归。

| 本轮命令/证据目录（均 ignored） | 实际结果 |
|---|---|
| 单项 `python3 -B -m unittest discover -s experiments/harness/dsh -p test_probe.py -k timeout_kills -v`，`timing-evidence/dsh-{1..5}.log` | 连续 5 次 exit 0，无 skip |
| 默认入口 `timing-default` | 进程/JSON 均 exit 2，无 fail |
| `--host-process-tests` 入口 `timing-host` | 进程/JSON 均 exit 2；HB03 真实 probe、3 项真实进程回归、5 项 VM host 回归通过 |
| `--loopback` 入口 `timing-loopback` | 进程/JSON 均 exit 2，仅 sandbox/pass 与 loopback-policy/blocked |
| 上述默认/host 的 `engineering.py check` | 两次 exit 0，真实 Swift/Rust 编译及工程回归通过 |
| `python3 -B scripts/check-baseline.py` | exit 0 |
| `python3 -B tests/fixtures/csv/check.py` | exit 0 |
| `python3 -B -m unittest discover -s tests -p test_environment_gate.py -v` | 6 项通过，含真实受控地址拒绝验证 |
| `python3 -B -m unittest discover -s experiments/harness/dsh -v` | 最终 10 项通过；包含新增启动失败负向回归 |
| `git diff --check` | exit 0 |

三个入口均 `scratch_removed=true`、`residual_scratch_processes=[]`、`process_observation_exit=0`。单项及全套 dsh 临时目录由 TemporaryDirectory/finally 删除；最终 `timing-evidence/cleanup.json` 未发现 HB03、fake-QEMU/QEMU、helper 或 dsh fixture 进程，launchd 观察无 `dev.havenbox.runtime` 项；首次文本 grep 仅匹配观察命令自身，未据此执行任何 kill。未运行真实 VM、模型、Keychain；既有资产与产品验收阻塞不变。三个改动 Python 文件主动 LSP 均 inconclusive，不能宣称静态检查通过。

## HB02/HB11 真实 VM 复验（本轮最新）

本节覆盖前文“当前 worktree 无 tools”和“真实 VM 尚未执行”的历史状态。未下载资产、修改运行时 pin、签发 ValidationRunGrant 或提交 Git；仅从同仓库干净的 `hb11-runtime` 工作树以 APFS clonefile 复用 ignored 资产。源工作树的全部固定文件哈希匹配；原 `/Volumes/UTM` 来自 `cod` 工作树，确认没有相关运行进程后卸载，再由当前工作树的同一固定 DMG 只读挂载。当前 `experiments/vm/replay-hb02-local.sh verify` 退出 0，UTM 5.0.8 / QEMU 10.0.12 / HVF 可用。

| 实际命令/证据 | 结果 |
|---|---|
| `experiments/vm/replay-hb02-local.sh start`，检查 guest 行，`stop` | exit 0；随机 nonce 握手，`root_readonly=true`、`network=loopback-only`、`shared_mounts=none`、`persistence=present`；verified process group 停止 |
| `experiments/vm/replay-hb02-local.sh kernel-probe-start`，检查 guest 行，`stop` | exit 0；cgroup v2 `cpu,memory,pids`、admission、`cgroup.kill` 空组、mount/network/IPC namespace、UID/GID 10001 均通过；verified process group 停止 |
| `SDKROOT=...MacOSX26.5.sdk guest/image/build-supervisor.sh` | exit 0；AArch64、static、stripped，无 INTERP/DYNAMIC，GNU_STACK 为 RW |
| `guest/image/build-image.sh ... --output experiments/vm/assets/hb11-candidate-1` | exit 0；全新 candidate，root 543 个、data 3 个可见 inode 均为 0:0，最终 e2fsck 通过，镜像为 0444 |
| 七项 `probe-hb11-channel.py`，日志 `.build/test-environment/hb11-channels-1/` | 全部 exit 0；harness/worker normal 均有 HelloAck、SyntheticTaskCompleted、Stopped；lease/replay/device-drift/output-overflow/active-lease 各终态断言为 true；每次独立 0600 data 克隆已删除 |
| `run-hb11-runtime.sh ... normal` / `lease-expiry`，日志 `hb11-runtime-1/` | 均 exit 0；真实 RuntimeManager→launchd→helper→QEMU→guest 通过；lease 路径两次 `stage=lease-rejected` 且保持 terminal |
| `verify-hb11-kill.sh ... ui/helper/qemu`，日志 `hb11-kill-1/` | 三项均 exit 0、`cleanup=true`；只强杀当前 instance 对应目标 |

两次 HB02 停止后 `experiments/vm/state/qemu.pid` 不存在。三种强杀对应运行目录均不含 `launch.json`、launchd plist、FIFO 或三个 socket；最终 `dev.havenbox.runtime.*` job 和本工作树 helper/QEMU 进程列表均为空。完整 qemu 日志保留在上述 ignored 证据目录；强杀脚本自身保留其运行目录和日志供复核。

首次 supervisor 构建未指定 SDKROOT，宿主默认 SDK 27.0 与链接器不兼容而退出 101；按固定 SDK 26.5 重跑成功，没有改工具链。首次宿主 Swift 构建在 Orca 的 x86_64 shell 下让通用 swiftc 进入 Rosetta，因 llbuild 仅 arm64 而 SIGABRT；最小复现 `swiftc --version` 连续三次退出 134，`arch -arm64 swiftc --version` 成功，之后以 `arch -arm64` 执行原构建链成功。这是宿主进程架构选择问题，不是 Swift 源码或 VM 缺陷。

本轮完成的是 HB02/HB11 固定 synthetic candidate 的真实机制复验，不自动修改 HB 验收状态，也不覆盖普通用户入口、真实 harness/模型/MCP、Keychain 或精确 loopback sandbox；这些剩余边界继续按前文和 PRD 验收。

