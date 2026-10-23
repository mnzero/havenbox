# Git 提交规范

仓库采用 [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/)，校验规则保存在 [.commit-conventions.json](../.commit-conventions.json)。提交钩子通过 Python 3 标准库执行，不依赖 npm。Python 3 和 Git 必须在提交环境的 PATH 中可用。

## 消息格式

```text
<type>(<scope>): <描述>

<可选正文，解释原因和影响>

<可选页脚>
```

scope 可选，使用小写字母、数字及 `.`、`_`、`/`、`-`，例如 `architecture`、`agents`、`gateway`。标题最多 100 个字符，支持中文；正文与标题之间空一行。

| type | 用途 |
| --- | --- |
| feat | 新功能 |
| fix | 缺陷修复 |
| docs | 文档 |
| style | 不改变行为的格式调整 |
| refactor | 不改变外部行为的代码重构 |
| perf | 性能改进 |
| test | 测试 |
| build | 构建与依赖 |
| ci | 持续集成 |
| chore | 工具、配置与维护 |
| revert | 撤销已有提交 |

示例：

```text
feat(gateway): 增加推理请求配额校验
fix(storage): 保留跨会话成果引用
docs(architecture): 完善停止与恢复方案
chore(git): 配置提交消息校验
```

破坏性变更使用 `feat(api)!: 修改接口` 或在页脚填写 `BREAKING CHANGE: 迁移说明`。校验器检查标题格式、类型、scope、长度和正文空行，不判断业务含义或迁移说明是否充分。Git 默认的 `Merge ...` 与 `Revert "..."` 标题保留兼容；`fixup!`、`squash!`、`amend!` 临时标题未列入允许格式。

## 启用当前检出

从仓库内运行：

```sh
git config --local core.hooksPath .githooks
git config --local commit.template "$(git rev-parse --show-toplevel)/.gitmessage"
```

本地 Git 配置不随 clone 分发，新检出需执行一次。已有 hooksPath 时先检查并整合已有钩子，避免覆盖其他校验。若移动检出目录，重新设置模板的绝对路径。

[commit-msg 钩子](../.githooks/commit-msg) 在创建提交前校验消息，失败时拒绝提交并输出修正提示。可使用 `git commit` 打开带注释的模板，或使用 `git commit -m "docs: 更新文档"`。模板注释由 Git 的消息清理规则处理。

## 手动验证

将候选消息保存为文件后执行：

```sh
python3 scripts/check-commit-message.py /tmp/commit-message.txt
```

退出码 0 表示格式通过，非零表示失败。钩子使用同一脚本与配置。本地钩子可以被 Git 的跳过选项绕过；当前尚未配置服务端或 CI 提交消息检查，不能把本地校验描述为不可绕过的仓库策略。依据：[Git hooks 文档](https://git-scm.com/docs/githooks)。
