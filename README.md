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
