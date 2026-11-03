WPK 已结束牌局离线复盘导出

用途
本包仅处理已经解码并保存在本地的历史消息，输出脱敏的已结束牌局。
不连接 WPK、不采集实时牌桌、不提供下注建议、不发送下注操作。
不包含 WPK 客户端代码、原始抓包、Cookie、令牌或会话密钥。

运行环境
使用 WSL2 Ubuntu-24.04 的 Linux Node.js 24；无依赖，无需 npm install。
当前开发环境的 /home/ubuntu/Project 挂载只读，因此源码制作与测试在 WSL 原生 /tmp 中完成。
解压后的源码请放在 WSL Linux 文件系统，勿在 Windows 挂载盘中开发。

验证
  node --test test/*.test.mjs

将已经解码的本地历史消息转换为复盘文件
  node export-offline.mjs /path/to/decoded.json YOUR_NUMERIC_USER_ID /path/to/review.json
输出文件必须不存在，避免覆盖已有文件。
decoded.json 为数组；每项包含 time、direction 和 message。
time 为 ISO 时间；direction 为 received；message 包含 msgType 和 msgBody。
本转换器不解密 WebSocket 密文；输入需已经完成合法会话中的历史解码。

输入通知
  C_updateRoomNotify：房间、座位上限及盲注信息
  WP_bankerChangeNotify：本次样本中的新手边界
  WP_dealNotify：手号及已经解密的自己的手牌
  WP_roundChangeNotify：街道及本次新增公共牌
  WP_actionNotify：服务器已确认的操作和源 actionId
  WP_actionHistoryNotify：重连后的行动历史回填
  WP_playResultNotify：结算通知
  C_playLogNotify：一手最终牌谱

输出格式 wpk-offline-review-v1
  completed：有结算通知及最终牌谱，并通过一致性校验的手牌。
  unfinished：存在发牌但尚无最终牌谱的片段，settlement 恒为 null。
  rejectedFrames：未解析成功帧的时间和方向，不输出原始密文或认证信息。
  playerId/tableId：导出内稳定别名；hero 表示本账号，不保留真实用户和房间 ID。
  heroCards/board：例如 As、Th、6c、Kd。s/h/c/d 分别是黑桃、红桃、梅花、方块。
  未公开底牌为 null；对手公开底牌仅放在 revealedAt=settlement 的字段中，不能提前用于回放。

操作金额
  amountDelta：此次投入增量；PAY_MUSHROOM 表示转入独立蘑菇池，不计入普通下注。
  amountTo：操作后本街累计投入，历史回填缺少此字段时保留 null。
  amountReported：原通知中的 actionScore，不因它为零而判定字段缺失。
  balanceAfter：通知明确提供时保留；历史回填未提供则为 null。
  CHECK/FOLD 的 amountDelta 为 null，其 amountReported 保留实际通知值。
  WP_raise 请求可能被服务器确认为 BET；本导出以服务器确认类型为准。
  例：本次全下样本的增量是 214，本街累计投入为 338，二者不可混用。

结算及底池
  players[].netChips 来自最终牌谱 changeScore，并与结算通知 addScore 交叉核对。
  不将结算通知 changeScore 直接解释为净收益：它与 addScore 的含义不同。
  mushroom.paid/awarded/poolChange 分别为本手蘑菇缴费、奖励、池子留存变化。
  玩家净变化之和加蘑菇池变化应为零；校验失败时拒绝导出。
  totalPotReportedByHistory 仅保留最终牌谱的原始 totalPot，不宣称其等于所有边池与蘑菇池之和。

边界和校验
  下一手费用可能早于上一手最终牌谱到达；按新手边界归属，不能仅按牌谱到达时间分组。
  重连行动历史按 sourceEventId 回填和排序，缺少的余额/目标金额不补造。
  重复源动作冲突、源序号缺口、非法牌码、重复已知牌、金额非安全整数、
  公共牌与最终牌谱不一致、费用不一致、结算不一致都会抛错。
  这是当前样本验证过的单桌转换器；不支持换桌混流、双次发牌结算或任意协议版本。
  未进行平台规则全覆盖验证，不输出可信策略或行动建议。

样本
sample/hands.json 来自本次已采集历史，已去除账号、昵称、头像、凭据和真实标识。
测试使用纯合成数据；真实样本无需提交到 Git 或共享训练。
GitHub 仓库未修改、未提交、未推送；未接入实时前端或策略服务。
