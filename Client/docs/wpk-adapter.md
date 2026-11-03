# WPK 适配器设计

0.3 已将原型纳入 [evidence/wpk](../evidence/wpk/README.md)，并实现当前 MV3/WASM 适配器；字段映射和可用接口请以 [wpk-business.md](wpk-business.md) 为准。以下保留此前研究入口说明。

状态：此前本机会话已验证页面解码后消息入口，本仓库 0.3 已移植业务采集器。所有私有名称仅作为版本特定证据和实现起点，必须启动时探测；不将网站私有模块视为稳定公开 API。

## 已知入口与限制

先前原型通过 `HMFClient.sharedInstance()._dispatchMessageToCC` 观察页面解码后分发，再读取 `WPGameData.Instance` 的本账号可见状态。可由 CDP Runtime 安装等价最小适配器，并用运行时 binding 传递白名单 DTO。每个执行上下文只允许一个适配器，安装带版本与停止清理；原方法必须恰好调用一次并保留返回/异常。

页面消息分发不一定等同底层 WS 接收；异步手牌解码可能晚于相关通知。适配器应验证实际解码完成时机，不因分发时读不到牌就导出空手牌替代已知值。网络原始二进制含应用层加密和 Protobuf，仅 CDP 抓取不能自动获得明文。首版复用页面合法解码，不复制密钥到扩展。

先前观察到的消息类别包括 `WP_actionNotify`、`WP_actionHistoryNotify`、`WP_userOptNotify`、`WP_roundChangeNotify`、`C_playLogNotify`、`C_updateRoomDetail`、`C_closeRoomNotify`。具体信封和字段以复现样本校验，不能凭名字填字段；客户端枚举不一定列出所有动态通知。

## 规范化要求

| 输入语义 | 规范事件/字段 | 必须保留的限制 |
| --- | --- | --- |
| 发牌与自己已解码手牌 | hero_cards | 对手未公开牌不读不传；未观察到为 null |
| 公共牌更新 | board_updated | 验证牌数量与重复牌，不能推断漏掉的牌 |
| 自己/他人的已确认行动 | action_confirmed | 保留 source_event_id、增量、本街累计原字段 |
| 本地发出的行动请求 | action_intent | 服务端可能把 raise 请求确认为 bet，不能提前定性 |
| 轮到谁及允许操作 | action_requested | 操作后立即清除旧 acting/options；摊牌和手间无可行动状态 |
| 跟注与加注范围 | call_amount / min_raise_reported / max_raise_reported | 未确认增量还是 raise_to 前不转换为下注指令 |
| 历史回填 | 源 ID 去重与冲突检查 | 接收到历史不证明整手完整；差异须阻止继续归约 |
| 结算与房间关闭 | settlement / room_closed | 分开记录明细、最后手数、未解释差额和最终对账状态 |

`WP_roundChangeNotify` 先前出现 `userAciton` 拼写；按实际 schema 处理，不能自动“修正”输入字段名。新手费用可能早于正式发牌，应归于明确的新手或 pending 作用域，不能计入上一手。

## 完整性与规则覆盖

本手中途附着、跨页面刷新、断线、Worker 漏捕、未知消息、语义冲突或未验证适配器版本都影响 coverage。`history_complete` 至少要求看见手开始和源动作连续，但仍不证明权威规则状态完整。

目标玩法包含多人、第三盲/蘑菇等原型规则尚未覆盖项。Rust reducer 重建观察状态，不冒充服务器权威规则引擎；未覆盖时可提供已验证的可见牌和行动数据，策略保持 unsupported。前端不发送自动下注操作。

## 样本与测试

本仓库仅提交合成 fixtures。后续从授权会话提炼结构样本时，先移除账号标识、昵称、Cookie、Token、密钥及未公开对手牌，再对照原始业务事件与 UI 验证字段。测试包含：新手费用先到、重复动作、乱序历史、换桌/换账号、旧 turn 清除、异步手牌解码、三街公共牌、最后一手与房间关闭、未解释积分差额。

安装失败显示 adapter_unsupported；用户停止后清除绑定与包装。采集异常不得改变原页面行为；不能为了恢复采集自动操作牌桌。
