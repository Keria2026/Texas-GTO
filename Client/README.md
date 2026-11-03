# TGTO Frontend

可加载的 Chrome / Edge Manifest V3 研究采集扩展：**chrome.debugger + Rust/WebAssembly + IndexedDB + 后端 WebSocket**。

## Pr1.0

当前扩展数字版本为 0.8.0，配套同仓库 Serve。支持当前下注轮起点搜索、画像来源与独立比较展示；算法和真实样本训练发布见 [Pr1.0](../docs/pr1.0.md)。原有采集功能如下。

- 双向逐条 WebSocket 消息采集，覆盖已识别的允许端点；关联 Worker / iframe 目标递归附着与恢复执行。
- Rust/WASM 执行消息边界、连续序号、ACK 范围和存储预算检查；失败时停止并显示覆盖缺口。
- 扩展来源 IndexedDB 严格事务：不可变记录、outbox 和确认进度；先落盘后发送。
- WebSocket 鉴权、能力协商、单批次在途、持久 ACK、断线退避重传、心跳与错误 ACK 拒绝。
- React 侧栏：开始/停止、后端连接、队列状态、最近逐条元数据和本地导出。关闭侧栏不停止采集。
- WPK 业务适配器读取本人手牌、公共牌、确认行动和配对结算；Rust/WASM 归约与校验，独立业务序号和 ACK。
- 原始原型、来源哈希、字段形状依据现已纳入 [evidence/wpk](evidence/wpk/README.md)。
- 已对接 Texas-GTO Python 后端：双流持久确认、服务器观察状态回传与侧栏覆盖提示；保留 SQLite Node 参考接收器。

**业务适配复用网站已解码的可见状态，不导出密钥。最新 WPK 尚未现场复测；策略和训练仍由原研究后端负责，本仓库不实现目标规则的策略覆盖。**

## 构建与加载

源码、依赖、Git、构建和测试统一在 WSL2 Ubuntu-24.04 的 Linux 原生文件系统执行；Windows 只运行浏览器客户端。要求 Node 24+、Rust 1.90+。

```sh
cd /home/ubuntu/Project
git clone https://github.com/Keria2026/Texas-GTO.git
cd Texas-GTO/Client
rustup target add wasm32-unknown-unknown
npm ci
npm run build
```

在 Edge `edge://extensions` 或 Chrome `chrome://extensions` 开启开发者模式，加载已解压扩展目录 `dist/`。Windows 可选择对应 WSL 路径，例如 `\\wsl.localhost\Ubuntu-24.04\home\ubuntu\Project\TGTO-FronEnd\dist`。无需远程调试启动参数，无需常开 DevTools。

从 0.2 升级时保留同一扩展目录和扩展 ID，覆盖构建文件后点击“重新加载”，先不要移除扩展。更换目录可能改变扩展 ID、数据库来源及后端 Origin；请先导出旧记录。原 IDB v1 自动迁移至 v2 并保留档案，新增服务器绑定设置；业务流使用新的独立源。

打开 WPK 页面，点击扩展按钮打开侧栏，选择标签页并开始采集，然后进入对局以建立新的游戏连接。附着之前的消息无法补取；无法识别端点的既有连接不会绕过白名单。页面导航会停止采集并记录缺口，需要人工重新开始。

默认仅保留逐条元数据。原始载荷需在开始前单独勾选，可能含敏感信息，仅存本地，不进入后端传输。导出本地载荷另有确认提示。

## Python 后端和服务器地址

正式源码已整合在 Texas-GTO 的 `Client/`。在仓库根目录运行：

```sh
uv sync --frozen
uv run --frozen texas-analysis
```

默认本地模式无需访问令牌，也无需填写扩展 ID。加载扩展后，后台自动连接 `ws://127.0.0.1:8765/v1/research/stream`；服务离线时自动重试，浏览器重启后自动恢复连接。开始采集仍需手工点击；规则与策略未覆盖时保持明确提示。

服务器部署包可使用 `TGTO_BACKEND_URL='wss://你的实际域名/v1/research/stream' npm run build` 固定地址、权限与 CSP；默认构建仅允许 8765/8766 loopback。公网 TLS／部署仍需单独完成。详见 [Python 对接说明](https://github.com/Keria2026/Texas-GTO/blob/dev/docs/tgto-integration.md)。

## 本地参考接收器

扩展管理页取得扩展 ID 后，在 WSL 中运行：

```sh
TGTO_ORIGIN='chrome-extension://YOUR_EXTENSION_ID' npm run backend
```

将 YOUR_EXTENSION_ID 替换为实际 ID。接收器监听 `127.0.0.1:8766`，只接受该精确 Origin；令牌保存在 `artifacts/reference-backend/session.token`，不打印令牌本身。该接收器仅供带显式配置的自动化测试；日常使用直接连接 Python 8765，侧栏不提供本地令牌输入。

8765 保留给现有研究后端；网络流要求 `network_events_v1`，业务流要求 `capture_events_v1`。后端仅支持其中一类时，另一类保持本地待确认，不阻塞支持的流。参考接收器不是算法或训练服务，也不会改动原有研究服务。

## 验证

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
npm run build
npm run typecheck
npm test
npm run test:provenance
# 仓库根目录已完成 uv sync --frozen 后，在 Client 中运行：
npm run test:python
npx playwright install --with-deps chromium
npm run test:browser
```

浏览器测试使用本地合成 WebSocket 服务和单独测试构建，不访问真实账号。生产包不包含测试目标开关。仓库根目录的 CI 在每次 push 和 pull request 检查后提供 `Serve`、`Client` 两个构建产物。

## 文档

- [运行和协议说明](docs/runtime.md)
- [业务适配与字段依据](docs/wpk-business.md)
- [业务协议](docs/protocol.md)
- [总体架构](docs/architecture.md)
- [WASM 实现边界](docs/wasm.md)
- [WPK 适配计划](docs/wpk-adapter.md)
- [实施阶段](docs/implementation.md)
- [验证记录](docs/validation.md)

浏览器退出后只能恢复已提交记录，不能补采退出期间的消息。浏览器重启后自动连接本地算法端，新的页面采集仍需人工启动。未确认记录不会因 ACK、重连或停止操作被静默删除；用户清理浏览器数据或卸载扩展仍会删除本地数据库。

### 0.7.0 八人行动建议预览

本地 Python 服务可经协商推送八人局已覆盖节点的 Pluribus 平均策略，侧栏显示行动、投入金额、加注总额与动作频率。金额、手牌、行动、蘑菇账本或模型覆盖不足时显示原因；断线、下一动作和到期会清除建议。

0.6.1 防止迟到的侧栏刷新恢复旧建议；配套 Python 服务在计算期间继续接收新事件和心跳，只保留最新待算状态并取消过期任务。原 0.6.0 推荐协议兼容。

0.7.0 新增 `capture_search_v1`，应与新 Serve 配套更新。侧栏区分搜索结果与已训练的基线，并显示缺覆盖／搜索预算不足的原因。

这是两手视野的分桶策略预览；尚未完成八人实局质量验收，翻后可协商全范围限时搜索，仍不含画像调整，不覆盖所有筹码、re-straddle 或 4–7 人参与的局面。未覆盖时不会生成替代建议。打开插件仍自动免令牌连接本地服务；更新后重载扩展，并在已入座的 WPK 牌桌开始采集，等待完整新手和可信结算链。
