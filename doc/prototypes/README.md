# Havenbox V0.5 可点击原型

此目录保存会话式工作台的可编辑源稿、独立页面和检查记录。所有模型、网络授权、进程、文件与恢复状态均为模拟；不调用真实服务，不接收真实 API Key。

## 打开与修改

入口为 [index.html](./index.html)。可直接用现代浏览器打开；本轮实际检查通过本地 HTTP 预览完成。

从仓库根目录启动预览：

```sh
python3 -m http.server 8766 --bind 127.0.0.1 --directory doc/prototypes
```

在浏览器打开 `http://127.0.0.1:8766/index.html`。服务仅提供本目录，不能用于公开部署。终端中按 Ctrl+C 停止。

修改 [havenbox-v05.fragment.html](./havenbox-v05.fragment.html)，再生成页面并运行状态检查：

```sh
node doc/prototypes/build.cjs
node doc/prototypes/check.cjs
```

本轮使用 Node.js 22 验证，无第三方依赖。`index.html` 为生成文件，不单独编辑。构建保留中文页面语言、移动端 viewport 和禁止外部请求的 CSP；样式和脚本都在本地页面内，不依赖 CDN 或 Codex 的可视化运行时。

状态检查使用 Node VM 与最小 DOM 替身，验证原型事件与状态约束，不启动浏览器。浏览器检查结果与复核步骤见 [检查记录](./verification.md)。

## 目录

- `havenbox-v05.fragment.html`：唯一可编辑原型源稿。
- `index.html`：可独立打开的生成页面。
- `build.cjs`：无外部依赖的页面生成器。
- `check.cjs`：可重复运行的状态回归检查。
- `verification.md`：实际浏览器检查、问题修正与未验证范围。
- `screenshots/`：本轮实际页面截图。

刷新页面会重置演示状态。设置中的“模拟重启”仅用于审阅恢复动线，不是持久化实现。项目功能与安全验收仍以 [PRD](../product-requirements.md) 为准。
