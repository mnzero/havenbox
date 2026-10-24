# 正式构建依赖清单（HB-08 candidate）

机器清单以 [`config/runtime-manifest.json`](../config/runtime-manifest.json) 的 dependencies 为准；此表定义交付位置和选择门槛，不是安装脚本或锁文件。正式工程由 HB-09 建立，本次不新增 Cargo/Swift/npm 空工程，也不安装依赖。

| 交付位置 | 依赖 | 当前依据 / 必须补齐 |
|---|---|---|
| 构建机 | Swift/Xcode/SDK、Rust、Node、Python | 精确版本、目标系统、锁文件和构建命令待核；不照抄 HB-01 未核实的 Swift/Rust 字符串 |
| 宿主包 | SwiftUI/AppKit/Keychain 系统 API | 最低 macOS 候选 14.0，参考 arm64/16 GiB；不得把 26.6 smoke 倒推为 14.0 已通过 |
| 宿主包 | QEMU、qemu-img、HVF entitlement、watchdog | HB-02 仅测 UTM 5.0.8 的 QEMU 10.0.12；提取后二进制、动态依赖闭包、构建配置/commit、entitlement、许可和再分发待核，不能把 UTM DMG 哈希当 QEMU 二进制哈希 |
| 宿主包 | SQLite | 必须带 WAL-reset 修复；官方 3.51.3 说明已核对，但没有选定本项目制品；不能仅做版本字符串 >= 比较或依赖未知系统库，需 exact source ID/制品 hash/修复依据及 WAL/backup 故障回归 |
| 宿主包 | llama.cpp/Metal、本地模型、模板/量化 | HB-07 缺资产，版本/许可证/模板/离线工具调用待选；资源独立离线包，不能运行期下载 |
| 来宾镜像 | Linux kernel、只读 rootfs、监督器 | HB-02 Alpine smoke 不等于生产镜像；最终 Linux 包闭包、架构、UID/cgroup/seccomp/namespace、许可证和 SBOM 待核 |
| 来宾镜像 | Kimi、Pi、dsh、对应 Python/Node、锁定依赖、适配器/补丁 | HB-05 Pi 1.3.0 为宿主协议探针，非来宾包；Kimi/dsh 待专家已提交证据；本工作不读取其未提交文件 |
| 来宾镜像 | CSV 引擎、CLI、MCP client/server 与 SDK | 双入口共用引擎；MCP 2025-06-18 仅候选协议，SDK 精确版本待实现核对 |
| 内部包 | host gateway / watchdog / guest supervisor / 内置 Agent | 自研产物也必须有版本、hash、许可证决定、SBOM；不能以“项目内文件”免除供应链检查 |
| 检查环境 | Python 标准库 | 本次仅 Python 3.15.0 实测；参考代码使用 3.10+ API，最低解释器兼容仍待 CI，不作为产品运行依赖 |

所有 entries 的 null 表示缺证据，而非通配符、不适用或已通过。源码/制品/补丁/依赖锁文件都需原始来源及 hash；许可证由实际分发闭包复核，不能猜测。SBOM 引用必须指向内部包实际内容，未知值阻止普通资格。没有上游凭据或业务数据写入清单。

内部包不增加 Developer ID、公证和自动升级范围，但 HVF entitlement 是真实运行要求，不等于无需签名检查。优先保留受管二进制已具备的 `com.apple.security.hypervisor`；必要 ad-hoc 重签是单独构建步骤，重签后重新 hash，并在最低系统与参考机重验。当前上游 [entitlements.plist](https://github.com/qemu/qemu/blob/master/accel/hvf/entitlements.plist) 只证明 key 名，不证明所选内部包 entitlement 正确。

选定/更新依赖后：更新 immutable revision 与制品 metadata → 重新计算契约文件 hashes → schema/回归 → 对新 binding 执行真实探针与集成验收 → 受审导入 receipts。不得把旧证据改写成新 binding；候选到冻结要保留失败历史。
