# TGTO 扩展与 Python 后端对接

正式采集前端在 [TGTO-FronEnd](https://github.com/Keria2026/TGTO-FronEnd) 的 `dev` 分支。已合并用户推送的 `2e72f34` 业务解析，并完成两条链路：

- 网络：Chrome CDP → WASM → IndexedDB → `network.append` → SQLite → `network.ack`。
- 业务：页面可见状态 → WPK DTO → WASM 归约 → 独立业务 outbox → `capture.append` → Python 校验与状态重建 → 持久 ACK → 侧栏显示服务器状态。

业务输入包含本人手牌、公共牌、玩家座位与可见筹码、确认动作、当前可见选项、结算与缺口。未知牌或金额保持 null，不生成模拟手牌、不读取对手未公开底牌、不上传本地原始网络载荷。旧结构探针保留作开发诊断，生产采集使用业务适配器，避免同时包装同一页面入口。

**数据链路已通过合成样例验证。目标 8 人／straddle／蘑菇玩法的策略模型仍未覆盖，因此服务器返回 `unsupported` 和空动作列表。** `history_complete` 是采集器观察标记，客户端报告的操作范围不是服务器规则引擎认证的合法动作；结算守恒通过也不等于完整规则已核实。既有 Pluribus 研究接口继续独立运行。

## 使用

在两个仓库切到 `dev`，先构建扩展：

```sh
# TGTO-FronEnd；Node 24、Rust 1.90+，安装 wasm32-unknown-unknown target。
npm ci
npm run build
```

在 Chrome 的 `chrome://extensions` 加载 `dist/`，复制实际扩展 ID。保持原安装路径和 ID，重新加载扩展即可升级；IndexedDB v1 原档案会迁移至 v2，新增服务器绑定设置，不删除原事件。

```sh
# Texas-GTO；Python 3.12、uv。
uv sync --frozen
TEXAS_ALLOWED_ORIGINS='chrome-extension://YOUR_ACTUAL_EXTENSION_ID' uv run --frozen texas-analysis
```

替换实际 32 位 ID，不使用通配符。默认监听 `127.0.0.1:8765`。自行读取 `artifacts/local/session.token` 并输入侧栏；不要放进 URL 或仓库。未预设 `TEXAS_API_TOKEN` 时重启会生成新令牌。

侧栏选择 `ws://127.0.0.1:8765/v1/research/stream`、连接服务、开始采集，然后在 WPK 入座。检查“业务适配已就绪”、“业务写入／后端确认”以及“服务器牌局与算法状态”。已在手中途接入会保留历史缺口。刷新页面或浏览器重启后需要人工重新开始采集。

Python 是实际研究后端；无需同时启动前端的 Node 参考接收器（8766）。参考接收器支持双流持久确认，但不提供 Python 的 `capture_state_v1`。

## 服务器部署构建

终端用户只安装扩展，Python 运行在服务器。服务器 DNS／TLS 和 [Caddy 示例](../deploy/Caddyfile.example) 配置完成后，构建固定 WSS 地址的扩展：

```sh
# TGTO-FronEnd；替换成实际服务器域名。
TGTO_BACKEND_URL='wss://analysis.example.com/v1/research/stream' npm run build
```

构建同时写入运行时地址、manifest HTTPS 权限和 CSP。服务器包仅允许该端点；不允许远端明文 WS、查询令牌、用户名密码或其他路径。服务端配置安装包对应的精确 Origin。当前是单个所有者令牌，不是多用户鉴权。公网部署、真实 TLS 和真实 WPK 牌桌验收仍未完成。

## 协议

HTTP health schema 保持 `1.1`；WebSocket 握手为 `1.2`。客户端：

```json
{"type":"auth","token":"USER_PROVIDED_TOKEN","capabilities":["network_status_v1","capture_state_v1"]}
```

服务器：

```json
{"type":"ready","schema_version":"1.2","capabilities":["network_events_v1","network_status_v1","capture_events_v1","capture_state_v1"]}
```

`network.append` 使用 `tgto.network/0.1`；`capture.append` 使用 `tgto.capture/0.1`。业务事件完整字段与前端保持一致，见 [前端契约](https://github.com/Keria2026/TGTO-FronEnd/blob/dev/docs/protocol.md) 和 [Python 校验模型](../services/analysis/src/texas_analysis/capture_contract.py)。每个流使用独立 source UUID 和从 1 开始的规范十进制 u64 字符串序号；每批最多 64 条、256 KiB。客户端单批次在途并轮换来源，避免网络数据阻塞业务流。

- 精确扩展 Origin、首帧令牌、5 秒鉴权期限、60 秒空闲期限；客户端 20 秒 ping。
- 业务事件拒绝未知字段、非安全整数、重复牌／玩家／座位、非法别名、对手私有选项和结算不守恒。
- 每个来源从序号 1 连续写入；相同序号同内容幂等，不同内容拒绝。事件、head 和观察状态在同一 SQLite 事务提交后才 ACK。
- ACK 仅确认提交批次的末尾，不因旧批次重传而确认更晚的 head。冲突、缺号或存储失败不确认、不部分提交。
- 状态按当前手重建；旧手结算单独归档，不能覆盖下一手。动作按源动作 ID 去重，补全已知值时拒绝冲突。缺口清除行动提示并标记历史不完整。
- WASM 在换手、街道或可见状态变化时补发完整 snapshot，使接收端能恢复未变动的玩家和历史标记。旧适配器仅首次快照的档案不能补出未记录状态，应使用新版本重新采集。

只有客户端请求了 `capture_state_v1`，业务 ACK 后才返回 `capture.state`：

```json
{
  "schema":"tgto.capture-state/0.1",
  "type":"capture.state",
  "source_id":"synthetic-source",
  "through_sequence":"51",
  "projection_version":"wpk-observation/1",
  "state":{"...":"完整观察状态，字段定义见 capture_state.py"},
  "analysis":{
    "status":"unsupported",
    "reason":"unverified_rules_and_strategy_coverage",
    "actions":[],
    "missing_fields":["verified_rule_profile","supported_strategy_model"]
  }
}
```

上面的 state 是结构示意，不能作为合法请求样例。完整可重放事件在 [合成 fixture](../fixtures/hands/wpk-observed-synthetic.json)。状态包括当前 scope、街道、牌、玩家、确认动作、观察选项、历史／缺口／关闭标记、最近 8 手结算和最多 16 条待配对证据。每手动作最多 256 条；完整事件档案保留。

客户端只显示已持久确认、与本地最新业务序号一致且属于当前活动会话的状态。停止、新事件、换源或断线使旧状态不再显示。重连后，若没有待传事件，等待下一条业务事件刷新服务器状态；此版没有独立查询恢复接口。旧批次重传不回传更新版本的状态。网络 `network.status` 仍仅说明该网络来源没有牌局字段，不能代替独立业务状态。

## 存储与验证

默认 `artifacts/local/capture.sqlite3`，WAL＋FULL，文件权限 0600。原 `network_events/network_heads` 保留；新增 `capture_events/capture_heads/capture_state`。作用域为 Origin＋流＋source＋sequence，不与合成 `sessions.sqlite3` 混用。每来源事件 JSON 限 64 MiB，每流总事件 JSON 限 256 MiB、256 个来源；索引、状态投影与 WAL 另有开销。没有自动清理和远端删除接口；容量满时拒收并保留客户端 outbox。换服务器地址会重排两类档案，同地址重连沿用确认进度。

```sh
# Texas-GTO
uv run --frozen pytest
npm run check
# TGTO-FronEnd，两个 checkout 均已安装依赖并完成 WASM 构建
TEXAS_GTO_ROOT=/absolute/path/to/Texas-GTO npm run test:python
# 需要允许加载未打包扩展的 Chromium；独立浏览器验收
TEXAS_GTO_ROOT=/absolute/path/to/Texas-GTO npm run test:python:browser
```

本次检查：Python 349 项、后端 npm 检查、前端 Node 41 项、Rust 8 项、离线原型 5 项和跨仓库 2 项全部通过；Rust fmt/clippy、前端 TypeScript/build、Python Ruff 与 12 份来源哈希检查通过。

跨仓库测试实际启动 Uvicorn，运行生产页面观察器、WASM、IndexedDB 和 WebSocket，覆盖丢失网络／业务 ACK、重传、多批次、后端重启、迟到结算、隐私字段排除和服务器状态版本。Python fixture 由前端 `node scripts/export-wpk-fixture.mjs` 生成，跨仓库测试逐事件检查其一致性。合成样例不能替代真实 WPK 的 200 手人工验收。

当前云环境不允许 Chromium 加载未打包扩展，Playwright 浏览器下载也受网络策略限制，浏览器测试未在这里重新通过；用户 `2e72f34` 的原环境验证记录保留在前端文档中，不作为本次合并后浏览器验收结果。
