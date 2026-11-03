# 0.3 WPK 业务适配与字段依据

## 已实现的数据路径

用户开始采集后，MV3 同时启动独立网络源与业务源。业务源通过 CDP Runtime 在允许标签页的主 frame / 默认 MAIN 上下文安装只读页面桥接，复用网站已经解码的业务数据，不导出加密密钥。

页面桥接仅访问白名单字段并将原玩家/房间 ID 替换为作用域别名；DTO 进入 Rust/WASM 归一化和 reducer；IDB 在同一 strict 事务写入业务事件、outbox 与检查点，提交后才更新内存。两个源使用不同 UUID 和各自连续序号，不能拿网络消息序号当业务序号。

浏览器/页面 API 仍由 TypeScript 调用；牌码、动作合法字段、源动作去重/冲突、街次归约、跨手归属、结算配对和积分守恒由 Rust/WASM 实现。JS 不维护另一套 reducer。

## 字段映射

| 来源字段 | 当前输出 | 依据和限制 |
| --- | --- | --- |
| HMFClient.sharedInstance()._dispatchMessageToCC | 通知入口 | 原 observer 第 4、116 行；原方法先执行一次，返回/异常行为保留 |
| __require('WPGameData').WPGameData.Instance | 可见状态入口 | 原 observer 第 5 行，属于私有 API，启动时探测 |
| CurrentUserInfo.user.userId / roomId | hero 身份 / table-N | 只在页面用于别名与身份变化检测，不传原 ID |
| sitUserList[].userId / seatNum / currentScore | player_id / seat / stack | 座位 1–8，整数筹码；未知为 null |
| 本人 sitUserList[].handCards | hero_cards | 只读本人；解码可能异步，500 ms 可见状态轮询补充 |
| publicCards | board | 只接受 0/3/4/5 张，重复牌或回退冲突拒绝 |
| currentBoutNum / round | hand_number / street | BET_BLIND、PRE_FLOP、FLOP、TURN、RIVER、THAN、CLEAN；未知 phase 标 unknown |
| WP_actionNotify / WP_actionHistoryNotify.actionList[].actionId | source_event_id | 规范 u64 字符串；相同 ID 不重复记账，冲突停止业务源 |
| actionType / actionScore | action / amount_delta | 服务器确认的类型；不是本地发出请求的推测类型 |
| seatScore / round | amount_to / source_round | 累计口径保持源报告；不能直接当合法 raise_to 指令 |
| WP_userOptNotify.msgBody | action_requested | userId、canActionList、callScore、minRaiseScore、maxRaiseScore |
| WP_roundChangeNotify.msgBody.userAciton | action_requested | 保留源拼写；行动者完成操作、换街、摊牌后清除旧选项 |
| WP_playResultNotify.thanList[].addScore | 结果净积分 | 与最终日志 changeScore 对照；**不能拿该通知的 changeScore 替代 addScore** |
| C_playLogNotify.handNum / handList1 | 最终手数与明细 | 与结果通知配对，支持上一手日志迟于下一手发牌 |
| handList1[].publicCardAll | 结算公共牌 | 可为 JSON 数组字符串，与已观察公共牌对照 |
| 本人 handList1[].handCard | 结算本人牌 | 与本人观察对照；不读取其他人的 handCard |
| handList1[].changeScore | 最终净积分 | 与同玩家 thanList.addScore 一致才可配对 |
| mushroomPayScore / mushroomScore | mushroom_paid / mushroom_awarded | 最终日志缺少 award 时使用结果字段；未知保持 null |
| C_closeRoomNotify | room_closed | 此前样本无 msgBody；关闭房间不证明最终账户对账完成 |

WPK 牌码为 `100*suit+rank`，suit 1/2/3/4 = s/h/c/d，rank 1=A、2..13=2..K。WASM 内部 canonical index 使用 rank 2..A 和 suit c/d/h/s，不能直接用 WPK 整数作索引。例如 205 → 5h → 14，101 → As → 51。对外业务事件保留标准牌字符串。

## 归约与结算规则

- 新手费用可早于发牌：先进入 pending_actions，在明确 WP_dealNotify 后归到新手并发出行动事件，不能计入上一手。
- 原动作序号重复时不重复记账；新增 round/累计字段发 action_updated；已知字段冲突拒绝本手并写缺口，继续监听下一次明确开局。
- history_complete 仅代表观察到边界且已见动作连续；不是权威规则完整性证明。八人、第三盲/蘑菇策略覆盖仍未验证。
- 结果通知/最终日志只有一方时发 settlement_pending 并持久保存待配对状态，不伪造结算完成。两方可任意顺序到达。
- 完整结算包含本人牌、公共牌、每位参与者净积分、独立池缴付/派发和配对状态。总净积分 + 池变化 = 0 才标 reconciled；缺失金额或存在差额标 unresolved，不能填 0 或猜测手续费。
- 公共牌、本人牌、已知金额或重复最终日志冲突为解析错误；双公共牌玩法明确 unsupported，不混成单牌面。
- 检查点只保留最近 8 手已完成结算/可见状态；完整事件档案继续在 IDB 中保留。未配对结果/日志最多各 8 手，超限停止，不静默覆盖。

## 生命周期与数据边界

网站模块懒加载时显示 waiting_client，未入座时 waiting_seat。500 ms 轮询仅补足可见状态异步变化，逐条行动与结算仍来自通知。只接受本次随机 binding 和已确认主 frame 默认上下文的输入，不接受 iframe、Worker 或未知上下文伪造的业务 binding。

0.8.1 中，座位短暂不可见会保存覆盖缺口并等待恢复，账号变化仍停止观察器；无效业务 DTO 被拒绝后保留原事件，等待下一次有效 `WP_bankerChangeNotify` 和发牌，不继续处理失败当手。有效的新手在同一业务来源恢复，不丢失其已记录画像。存储／WASM 运行失败、未知状态或账号变化仍停止，不能伪报已保存。根页面导航、用户停止和 debugger 脱离同时结束两源。worker 重启通过已授权采集意图恢复并标缺口；缺失历史不会自动变为完整。

可见手号先于发牌通知更新时，pending 费用及开局边界保留到明确发牌；不会因轮询快照提前消耗它们。侧栏版本读取 manifest，不再固定显示 0.6.1。后台连接关闭释放句柄，定时器与 alarm 共用同一连接，断线补传保持幂等。

默认不传 WPK 登录材料、昵称、原用户 ID、对手未公开牌或页面对象。原始网络载荷仍遵循本地显式开启规则，不加入业务事件。

## 实现与证据入口

- 原型和字段形状：[evidence/wpk](../evidence/wpk/README.md)
- 页面白名单与别名：[wpk-page.ts](../extension/wpk-page.ts)
- CDP 生命周期与上下文：[wpk.ts](../extension/wpk.ts)
- Rust 归一化、检查与配对：[wpk.rs](../crates/capture-core/src/wpk.rs)
- 合成完整牌局：[wpk-client.js](../tests/fixtures/wpk-client.js)
- WASM、隐私、冲突与事务测试：[wpk.test.ts](../tests/wpk.test.ts)

当前测试不登录真实 WPK，尚未对最新网站做现场复测。原型字段名称有依据，更新后的语义仍需现场对照，尤其是加注范围、累计投入和结束阶段额外积分池。
