# 连接 Texas-GTO Python 服务

本扩展的网络元数据现在可直接连接 [Texas-GTO](https://github.com/Keria2026/Texas-GTO) 的 `dev` 后端。完整契约、部署与算法边界见 [后端对接文档](https://github.com/Keria2026/Texas-GTO/blob/dev/docs/tgto-integration.md)。

## 本地联调

先构建并加载扩展。在 Texas-GTO checkout：

```sh
uv sync --frozen
uv run --frozen texas-analysis
```

本地默认服务免令牌、只监听 loopback，接受格式合法的 Chrome 扩展 Origin；无需配置安装 ID。后台在安装、浏览器启动及 worker 恢复后自动连接 8765，服务中断自动重试。手工断开在当前浏览器会话内有效。自动连接不自动附着 WPK，页面采集仍由用户点击启动。

本版新增 `network_status_v1`：首消息请求该能力，服务端 ready 公布支持后才接受 `network.status`。旧 Node 接收器只返回原有 ACK，侧栏显示其未提供算法接入状态；Python 服务返回当前源、已持久确认序号、`blocked / business_events_unavailable`、缺失字段及空动作列表。它不是扑克分析结果。

侧栏只展示当前活动采集源、最新本地序号对应的服务器状态；旧批次、换源、停止、断网时失效。早于 ready／未协商／未确认或超前的状态均拒绝。IndexedDB v2 增加已选服务器地址，升级或更换地址会从保留档案重建 outbox，仅重传元数据；同地址重连保留进度。该事务不会删除档案或上传 raw payload。

## 远端服务器安装包

```sh
TGTO_BACKEND_URL='wss://analysis.example.com/v1/research/stream' npm run build
```

替换成实际域名，生成 `dist/` 后加载扩展。此配置同时决定运行时唯一服务地址、CSP 和精确 HTTPS host 权限；无需用户电脑运行 Python。服务器需要 DNS、TLS 代理和精确扩展 Origin，示例配置在后端仓库 `deploy/Caddyfile.example`。

默认构建自动连接 Python 8765，8766 保留为测试传输；设置 TGTO_BACKEND_URL 的构建只允许这一远端 WSS 地址，不接受远端明文 WS、查询参数、fragment、URL 凭据或任意路径。远程 WSS 构建仍需服务端鉴权，令牌只通过侧栏输入并存在扩展 session 存储。

尚未部署公网服务；服务器沿用单所有者令牌，未实现多用户账户隔离。WPK 业务明文适配、业务事件流、八人 straddle/蘑菇模型和实时行动建议尚未接通。上传成功只代表网络元数据已持久保存。

## 跨仓库测试

先完成前后端依赖与本扩展构建，再运行：

```sh
# 在仓库根目录先 uv sync --frozen；前端位于 Client/，默认使用父目录后端。
TEXAS_GTO_ROOT=/absolute/path/to/Texas-GTO npm run test:python
# 另行运行浏览器链路（需允许加载测试扩展的 Chromium）：
TEXAS_GTO_ROOT=/absolute/path/to/Texas-GTO npm run test:python:browser
```

这两个命令分别使用 8766、8767 本机测试端口：真实 Uvicorn、生产传输代码、实际 WASM、故障重传和真实 Chromium MV3 采集。浏览器命令需要已安装的 Playwright Chromium（`npx playwright install chromium`），或者通过 `PW_CHROMIUM` 指定支持扩展测试的 Chromium 可执行文件。

合成 71 条事件验证多批次、丢失 ACK、断网、后端持久重启、无重复记录、原始载荷不上传；浏览器测试验证 CDP → WASM → IndexedDB → Python → 接入状态侧栏及断线清理。跨仓库测试是显式命令，不会悄悄跳过缺失的后端环境。常规单仓库 `npm test` 仍独立运行。

本次云环境已通过 Node → Python 跨仓库测试。浏览器验收未完成：系统 Chromium 154 的管理员策略拒绝加载未打包扩展，Playwright 浏览器下载域名被网络策略拒绝；既有浏览器套件因此失败，新增 Python 浏览器场景仅完成语法检查，未执行成功。需要在允许测试扩展的 Chrome/Chromium 或 CI 环境运行上述浏览器命令。
