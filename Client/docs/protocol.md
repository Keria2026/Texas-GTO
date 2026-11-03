# 0.5 业务事件与服务器观察状态协议

当前实现位于 extension/model.ts、db.ts、transport.ts；事件内容由 Rust wpk.rs 的同一套严格字段校验处理。网络流继续使用 [tgto.network/0.1](runtime.md)，不能共享业务源 ID 或交付序号。

## 协商与消息

默认本地 Python 服务的 auth 首帧只协商能力，不要求 token；远程/显式令牌模式仍验证 token。持久化、ACK 和重传语义不变。

沿用 `/v1/research/stream` 的 auth → ready，schema_version 为 1.2。`capture_events_v1` 控制业务流，`network_events_v1` 独立控制网络流。后端至少支持一类即可连接；未支持的流保留 outbox 并在侧栏提示。

```json
{"type":"ready","schema_version":"1.2","capabilities":["network_events_v1","capture_events_v1"]}
```

```json
{
  "schema":"tgto.capture/0.1",
  "type":"capture.append",
  "source_id":"synthetic-business-source",
  "events":[{
    "schema":"tgto.capture/0.1",
    "type":"business_event",
    "adapter_version":"wpk/1",
    "source_id":"synthetic-business-source",
    "sequence":"1",
    "captured_at":"2026-11-01T00:00:00.000Z",
    "kind":"action_confirmed",
    "table_id":"table-1",
    "hand_id":"table-1:2",
    "data":{
      "source_event_id":"2007",
      "player_id":"hero",
      "action":"RAISE",
      "amount_delta":10,
      "amount_to":12,
      "source_round":"PRE_FLOP"
    }
  }]
}
```

示例为合成值。实际 source_id 为随机 UUID；业务 Session.parentSource 指向网络源，两源各自从序号 1 开始。sequence 为规范十进制 u64 字符串，JS 使用 BigInt。金额为 JS 安全范围内整数；未知为 null。

提交后确认：

```json
{"schema":"tgto.capture/0.1","type":"capture.ack","source_id":"synthetic-business-source","through_sequence":"1"}
```

最多 64 条/256 KiB 一批，单连接仅一个批次在途。源之间轮换，避免网络高流量饿死业务流。同源顺序不变。前端先原子提交事件、outbox 和检查点，再发送；服务端提交事务后 ACK。

断线重发原 source/sequence/内容，服务端同内容幂等、同序号不同内容拒绝并回滚。ACK 校验类型、schema、来源及当前发送范围，重复无副作用，倒退/超前/跨流确认拒绝。确认只删除 outbox，不删除事件档案。语义为至少一次传输 + 幂等提交。

## kind 与 data

| kind | data |
| --- | --- |
| snapshot | street、hero_cards、board、players、turn、history_complete、rules_version |
| hand_started | hand_number、history_complete |
| hero_cards / board_updated | cards，规范牌字符串；本人未知牌可为 null |
| players_updated | players：player_id、seat、stack |
| action_confirmed / action_updated | source_event_id、player_id、action、amount_delta、amount_to、source_round |
| action_requested | turn 或 null；turn 含 player_id、legal_actions、call_amount、min_raise_reported、max_raise_reported |
| settlement_pending | received 为 result/log，players 为已收到一方的结果行，尚未完成配对 |
| settlement | table_id、hand_number、hero_cards、board、players、status、reason、pool_change |
| room_closed | reconciled 固定 false，不推断账户最终对账 |
| capture_gap | reason；作用域不明时顶层 table_id/hand_id 为 null |

动作保留确认类型 PAY_MUSHROOM、SMALL_BLIND、BIG_BLIND、THIRD_BLIND、ANTE、BET、RAISE、CALL、ALL_IN、FOLD、CHECK。amount_to 和 min/max 报告值不被解释为通用合法 raise_to。

结算 players 行为 player_id、net_chips、mushroom_paid、mushroom_awarded。status 为 reconciled / unresolved，reason 为 null / missing_amounts / point_difference。两类通知未收齐时仅发布 settlement_pending，并持久保存待配对状态；[配对和守恒依据](wpk-business.md)。

## 数据与恢复

IDB v2 保留 sessions/events/outbox 原有键和记录，新增 settings 存储服务器绑定；v1 原位升级。换服务器会从档案重排两类事件，raw 不进入上传。旧 Session.stream 缺失视为 network；新 business Session 增加 parentSource 和 checkpoint。一次消息归约结果与检查点原子提交，失败不推进版本；源动作 ID 负责语义去重，交付序号只编号实际业务事件。

页面输入受主上下文和 binding 约束，WASM 拒绝未知字段、异常数值和冲突。数据不包含原账号/房间 ID、昵称、认证材料或未公开对手牌。检查点用于观测恢复与重放，不是权威规则状态。

worker 重启将活动源标 interrupted 并追加缺口；旧源不自动继续抓取，但可在鉴权后补传，重新开始使用新 epoch。参考接收器和 Texas-GTO 的 8765 Python 服务均已支持双流；Python 额外提供协商的观察状态回传。

## 服务器状态回传（0.5）

客户端 auth.capabilities 包含 `network_status_v1` 和 `capture_state_v1`。仅当 ready 声明 `capture_state_v1` 时，接收 `capture.state`（schema `tgto.capture-state/0.1`）：包含 source_id、through_sequence、projection_version=`wpk-observation/1`、state 与 analysis。完整契约与持久化说明见 [Python 对接文档](https://github.com/Keria2026/Texas-GTO/blob/dev/docs/tgto-integration.md)；前端在 capture-state.ts 校验响应并使用相同 WASM 校验牌、玩家、动作和结算。

state 是观察投影，不是完整规则状态；analysis 当前只能为 unsupported、reason=`unverified_rules_and_strategy_coverage`、actions=[]，并列出 missing_fields。未知筹码保持 null，原报告加注范围不转成猜测的 raise_to。

结果必须对应已确认业务源，超前确认、错误来源或未协商响应会断开并保留队列。旧批次被忽略，侧栏仅显示当前活动业务源且 through_sequence 等于本地 last 的状态；断线清空。无待传事件的重连等待下一条业务事件刷新，没有独立查询恢复接口。

WASM 在首次、换手及可见状态（含街道/历史完整性）变化时发送完整 snapshot。重复且未变化的观察不制造事件；新增快照让后端可重放每手的玩家与街道。它不补全接入前或采集缺口中的未知动作。

## 八人行动建议（0.6.1 预览）

额外协商 `capture_recommendations_v1`，只在双方声明后接收 `capture.recommendation`（schema `tgto.capture-recommendation/0.1`）。它与兼容旧客户端的 capture.state.analysis 分开；后者仍是观察诊断。

消息绑定 source_id、hand_id、through_sequence。ready 携带实际动作、策略频率、本次支付 amount_delta、加注到的本街总额 raise_to、selected_action 和固定模型版本；unavailable 的动作必须为空。模型目前仅允许 pluribus_blueprint、两手视野、质量未认证、不使用画像调整。缺平均策略不作均匀补齐。

只有当前活动业务源、本地最后版本且已经 ACK、相同观察状态／手牌、轮到本人和未过期时才能展示。业务变化立即清除；断线清空，200 毫秒到期检查，最长有效 10 秒。插件只展示，不自动下注。字段及边界由 recommendation.ts 严格校验。

0.6.1 保持 wire schema 不变。服务的推荐消息可与心跳／后续批次交错，ACK 不等推荐完成；旧计算取消后只处理最新待算版本。客户端仍按业务源和 through_sequence 过滤，侧栏所有刷新入口共享版本校验，不能让迟到的状态覆盖新结果。

采集意图只保存在当前浏览器的 session storage。后台 worker 重启结束旧 epoch 并留下缺口，随后仅恢复原授权标签页；显式停止会先清除意图。浏览器完整退出／页面导航不保证自动恢复，需重新开始采集。本手有缺口时不能推荐。
