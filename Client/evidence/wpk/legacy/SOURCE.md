# WPK 原型来源与字段依据

核对日期：2026-11-01。

原工作区 `/tmp/texas-gto-source`，origin 为 Keria2026/Texas-GTO，工作分支 dev，基线提交 0483bc26b3b065514e81df7e86fbf296d1374f51。
observer.mjs、stream.mjs 和观测 schema 当时是本地未提交修改，不存在于该远端 dev 树中；不是可以直接 checkout 的原型分支，也没有随 TGTO-FronEnd 0.2 发布。这里提供原文件副本，不包含真实账号、会话密钥或抓包。文档中的“此前已验证”应理解为此前会话本机验证，不是当前网站版本复测。

## 代码位置（行号按本包副本）

- adapters/wpk/observer.mjs:4–5：HMFClient.sharedInstance()._dispatchMessageToCC 和 __require('WPGameData').WPGameData.Instance。
- observer.mjs:11–13：CurrentUserInfo.user.userId、CurrentUserInfo.roomId，只用于作用域和自己身份判定，不直接导出原 ID。
- observer.mjs:20–24：WPK 牌编码，suit=floor(code/100)，rank=code%100；1/2/3/4 花色为 s/h/c/d，rank 1=A、2..13=2..K，0/-1/缺失为未知。例如 205→5h、210→Th。
- observer.mjs:33–52：sitUserList[].userId/seatNum/currentScore，自己的 handCards，publicCards，currentBoutNum 与 round。
- observer.mjs:58–70：通用观察快照输出字段及未知值口径。
- observer.mjs:87–103：WP_actionNotify / WP_actionHistoryNotify.msgBody.actionList[]；actionId、userId、actionType、actionScore、seatScore、round。
- observer.mjs:105–107：WP_userOptNotify.msgBody；WP_roundChangeNotify.msgBody.userAciton（原拼写）；userId、canActionList、callScore、minRaiseScore、maxRaiseScore。
- observer.mjs:116–122：先调用原分发方法，再观测状态；原型另有每秒读取可见状态的回退，不是纯逐条事件 reducer。
- adapters/wpk/stream.mjs:93–102：Runtime.addBinding / Runtime.evaluate 注入和清理；旧桥接使用外部 CDP，不是 MV3 实现。
- packages/protocol/test/live-observer.test.mjs：合成页面和动作 fixtures，不是真实会话原文。
- packages/protocol/src/index.ts:349 起：ObservedSnapshotSchema 的结构与跨字段校验。

## 向新 WASM 核心映射

TGTO-FronEnd 使用 rank 2..A = 0..12，suit c/d/h/s = 0..3 的位索引，与 WPK 十进制牌编码不同，必须显式转换。不能将 205 等编码直接作为 WASM 牌索引。

actionScore 在旧观察器中映射 amount_delta，seatScore 保留为 amount_to。seatScore、本街累计投入、minRaiseScore/maxRaiseScore 与 raise_to 的口径不能仅凭名称定死，必须对照实际动作和最新样本核查；字段无法确认时保持 reported/unknown。

## 结算数据位置

实时 observer 只在 WP_playResultNotify / C_cleanNotify 时清除旧 turn，没有完整结算明细解析，不能把它描述成已经完成结算映射。

另一个已交付文件 wpk-offline-parser.zip 中：
- wpk-offline-parser/src/offline-hands.mjs:37 起：按 C_playLogNotify 归并完整手数。
- :125–159：将 WP_playResultNotify.msgBody.thanList 与 C_playLogNotify.msgBody.handList1 合并，读取 changeScore、mushroomPayScore、mushroomScore，校验积分与独立池变化。
- sample/hands.json 为已脱敏的归并结果；不是完整原始协议。

wpk-session-ending.json 提供此前结束阶段的脱敏业务消息，events[].message 保留 msgType/msgBody 字段结构。包含最后 56/57/58 手的 WP_playResultNotify、C_playLogNotify，以及 C_updateRoomDetail 和 C_closeRoomNotify。不是 58 手完整原始结算档案。

已观察到的结构：
- WP_playResultNotify.msgBody: thanList、highPublicCards、isAllUserAllin、isDealDouble。
- C_playLogNotify.msgBody: handList1、handList2、handNum、frequency、insuranceGain、totalPot。
- C_updateRoomDetail.msgBody: buyScoreList、currentBoutNum、totalBuyScore、totalDealScore 等。
- C_closeRoomNotify 此次无 msgBody；房间关闭不等于最终对账已完成。结束样本仍有 720 积分差异未解释，不能擅自归为手续费。

## 如何使用

此 ZIP 是源码参考包，不是可独立安装的扩展。schema 依赖原仓库的 zod；stream 依赖原研究服务和旧协议，不应直接替换 TGTO-FronEnd 新的 IDB/网络队列。

若需还原完整旧工作区，使用另行交付的 texas-research-websocket.patch，在 Texas-GTO 的上述基线应用；它包含旧原型与研究通信的整体修改。移植时只提取已核查字段与合成用例，保留新扩展的浏览器宿主和持久化结构。
