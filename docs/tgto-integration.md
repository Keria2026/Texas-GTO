# TGTO 扩展与 Python 后端对接

本次实现的是可运行的网络采集接入：Chrome → CDP → WASM → IndexedDB → WebSocket → Python → SQLite → 持久确认与侧栏状态。正式采集前端仍在 [TGTO-FronEnd](https://github.com/Keria2026/TGTO-FronEnd)，本仓库的 `apps/extension` 继续用于算法研究。

**网络元数据不包含牌局状态。当前不从哈希猜测手牌，也不把网络序号当作牌局 stateVersion。** 本连接返回“业务数据缺失”，动作列表为空；既有 Pluribus 主线研究接口继续独立工作。`capture_events_v1`、WPK 业务适配和真实手牌的分析请求尚未实现。

## 直接连接 Python 服务

1. 在 TGTO-FronEnd 运行 `npm ci`、`npm run build`，在 Chrome 扩展管理页加载 `dist/`，复制实际扩展 ID。
2. 在 Texas-GTO 设置精确 Origin 后启动：

```sh
uv sync --frozen
TEXAS_ALLOWED_ORIGINS='chrome-extension://YOUR_ACTUAL_EXTENSION_ID' uv run --frozen texas-analysis
```

将占位 ID 替换成扩展管理页的 32 位 ID；不能使用通配符。默认服务在 `127.0.0.1:8765`，可用 `TEXAS_PORT` 更改端口。默认生成的令牌保存在 `artifacts/local/session.token`，只自行读取后输入侧栏，不放入 URL 或仓库。重启时未设置 `TEXAS_API_TOKEN` 会重新生成令牌，浏览器需重新连接。

3. 在扩展选择 `ws://127.0.0.1:8765/v1/research/stream`，输入令牌、连接、开始采集。Python 接口取代 Node 参考接收器，二者不是必须同时启动。
4. “后端已确认”表示元数据已持久化；“算法接入状态”单独显示业务数据缺失。默认依旧不上传本地原始载荷。换服务器地址时会从保留的档案重新排队上传元数据；同地址重连沿用确认进度。

## 服务器 WSS 构建和运行

终端用户仅安装扩展，Python 运行在服务器。给服务器配置 DNS 和 TLS 后，使用 [Caddy 示例](../deploy/Caddyfile.example) 将 WSS 代理到服务器本机的 8765。设置 Caddy 的 `TGTO_DOMAIN` 为实际域名。示例仅开放 stream 和 health，研究／训练 HTTP API 留在服务器本机。

```sh
# 在 TGTO-FronEnd 中，域名必须替换为实际地址。
TGTO_BACKEND_URL='wss://analysis.example.com/v1/research/stream' npm run build
```

构建同时写入运行时地址、manifest HTTPS 权限和 CSP；服务器构建只允许这一端点。禁止远端明文 WS、查询令牌、用户信息、fragment 和其他路径。服务器仍需配置安装包对应的精确 `TEXAS_ALLOWED_ORIGINS`。更换安装路径可能改变未打包扩展 ID，以实际 Chrome 管理页为准。

此版本沿用单个所有者的 `TEXAS_API_TOKEN`，不是多用户账户系统。不要把同一个服务令牌分发给不同用户；按用户撤销令牌、租户隔离、训练管理员权限与生产作业隔离仍未实现。尚未部署到公网服务器，也未验证实际域名 TLS；示例配置不代表部署完成。

## 精确协议与存储行为

HTTP health 的 schema 仍为 `1.1`；WebSocket 握手仍为 `1.2`；采集记录仍为 `tgto.network/0.1`，三者独立。

新客户端首消息：

```json
{"type":"auth","token":"USER_PROVIDED_TOKEN","capabilities":["network_status_v1"]}
```

服务器响应：

```json
{"type":"ready","schema_version":"1.2","capabilities":["network_events_v1","network_status_v1"]}
```

- 旧客户端不请求状态能力时只收到既有 ACK，避免未知消息导致停连。
- `network.append`：最多 64 条，UTF-8 整帧最多 256 KiB；十进制字符串 u64 连续序号，从 1 开始。
- Origin 必须是显式允许的 Chrome 扩展来源；令牌在首帧验证。鉴权期限 5 秒、已认证空闲期限 60 秒，客户端 20 秒 ping 保活。错误 Origin、凭据或协议不会确认事件。
- 严格校验字段、类型、时间、opcode、帧长和摘要；拒绝未知字段／原始载荷／带查询、fragment 或凭据的端点。
- `capture.sqlite3` 使用 WAL、FULL 同步、立即事务；按 `(origin, source_id, sequence)` 幂等。内容冲突、序号缺口、配额溢出整批回滚。ACK 始终是**本次批次尾序号**，即使持久 head 更大也不超前确认。
- 网络档案与模拟 `sessions.sqlite3` 分开；不会初始化随机手牌或触发训练。
- 配额为单 source 的规范 JSON 64 MiB、全部 source 合计 256 MiB、最多 256 个来源作用域。配额统计的是事件 JSON，数据库索引和 WAL 有额外开销；此版本没有自动清理与在线归档。达到配额停止接收并保留客户端队列。
- 默认 token 只建立单用户部署边界；Origin 隔离用于防止不同扩展来源碰撞，不能当作多用户授权。

ACK 后，仅对协商了状态能力的连接发送：

```json
{
  "type":"network.status",
  "schema":"tgto.network-status/0.1",
  "source_id":"synthetic-source",
  "through_sequence":"1",
  "status":"blocked",
  "reason":"business_events_unavailable",
  "actions":[],
  "missing_fields":["rules","hand_id","hero_cards","board","players","action_history"]
}
```

这是版本化接入状态，不是分析结果。客户端拒绝未确认／超前状态；旧批次状态被忽略；侧栏只显示当前活动采集会话且与最新本地序号一致的状态。新记录、停止采集、换源、断线或浏览器重启都会让旧状态失效。后续真正的分析结果必须新增独立契约，携带手牌和状态版本，不能复用这个序号。

## 验证

后端新增 25 项接入测试，完整 Python 回归共 329 项通过。验证非法 Origin／token、超限与二进制帧、字段泄漏、冲突、乱序、配额回滚、u64、重启及旧客户端兼容。

TGTO-FronEnd 提供跨仓库测试：

```sh
# 先完成两个仓库的依赖安装、前端 WASM/build，以及 Playwright Chromium 安装。
TEXAS_GTO_ROOT=/absolute/path/to/Texas-GTO npm run test:python
# 另行运行真实 Chromium 集成：
TEXAS_GTO_ROOT=/absolute/path/to/Texas-GTO npm run test:python:browser
```

已通过的跨仓库测试使用真实 Uvicorn、前端生产传输代码、WASM 与合成消息，覆盖丢失 ACK、多批次、后端重启和不上传原始载荷；20 项前端 Node 测试通过。独立浏览器场景用于验证 Chromium 扩展到 Python 的接入状态显示，但本次云环境未完成浏览器验收：系统 Chromium 154 管理员策略禁止加载未打包扩展，Playwright 下载域名也被网络策略拒绝。既有浏览器套件因此失败，新增 Python 浏览器测试仅完成语法检查，尚未运行成功。所有输入均为合成数据，不能作为真实 WPK 字段映射或目标桌策略覆盖的验收。

## 接下来需要的数据与实现

TGTO-FronEnd 0.4 已加入有界的页面分发入口结构探针，可在用户本机导出字段名和类型，见 [探针说明](https://github.com/Keria2026/TGTO-FronEnd/blob/dev/docs/wpk-probe.md)。这些报告仅保存在本地，不进入网络或业务 outbox；当前字段转换仍未实现。

仍需验证 WPK 已解码消息与公开状态的脱敏样本，尤其手牌号、座位/稳定作用域 ID、hero 底牌、公共牌、行动金额含义、主池/边池、straddle 和蘑菇结算。前端文档里的页面对象名称不足以确定字段语义。

在这些映射确认后新增独立业务事件流、可靠状态归约和真实分析请求。八人/straddle/蘑菇会话规则与匹配模型仍需独立扩展、训练和评测；当前 2–6 人研究模型不能直接作为目标桌推荐来源。此次接入没有变更既有求解器，也不代表完整 Pluribus 复现或真实桌最佳决策已经完成。
