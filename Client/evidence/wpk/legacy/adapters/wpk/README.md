# WPK 适配器研究入口

目标站点：https://h5.wpk.com/ 。本目录提供本地 HAR 结构分析器、研究牌谱导入契约和独立的 WSL 只读观测桥接。桥接读取客户端已经解包且本账号可见的状态，运行方法见 [WebSocket 科研链路](../../docs/research-websocket.md)。原始密文的离线解密不是桥接进程的输入方式。

## 研究牌谱导入

`@texas-gto/protocol` 导出 `WpkReviewSchema` 和 `WpkReview`，接收 `wpk-offline-review-v1` 脱敏复盘文件：

```ts
import { WpkReviewSchema } from "@texas-gto/protocol";

const review = WpkReviewSchema.parse(JSON.parse(fileText));
const firstHand = review.completed[0];
```

该契约接受 1–8 座位编号，保留自己的手牌、逐街公共牌、服务器确认的动作、源动作序号、金额增量、本街累计投入、结算净筹码与独立蘑菇池变化。未知对手牌和缺少的金额保持 `null`；公开对手牌标记为结算时可见，回放不能提前使用。

输入边界拒绝重复已知牌、重复或断裂的源动作、玩家与座位冲突、非安全整数金额、公共牌历史冲突，以及玩家与蘑菇池不守恒。当前格式为单桌复盘；对尚未结束的牌局只接受 `incomplete` 和空结算。额外字段（包括会话凭据）不被接收。对应 JSON Schema 为 `packages/protocol/schemas/wpk-review.schema.json`；其中运行时跨字段校验需继续由 `WpkReviewSchema.parse` 执行。

这不是 `SessionResponseSchema` 的模拟状态。现有模拟器限制为 2–6 人标准规则，不能将记录中的 8 座布局、第三盲或蘑菇规则静默转换为模拟会话，也不能据此标注已覆盖相应规则。此变更只准备前端的研究数据读取边界，不接分析请求、不改策略输出。

2026-11-01 本机采样验证了 8 手已结束牌局、164 条归并操作；第 4 手有 1 条行动历史回填，26 条早期密文未通过当前会话认证。真实样本仅用于本机验证，不加入仓库。样本表明下一手缴费可能先于上一手最终牌谱到达，归手不能只按牌谱到达时间。

## HAR 结构检查

```sh
uv run --frozen texas-wpk-inspect /path/to/wpk.har --out artifacts/wpk/endpoint-report.json
```

命令只读取本地文件，不发送网络请求。忽略请求头、Cookie、请求正文、URL 查询值与响应字段值；报告保留经过有限过滤的路由、字段路径与类型，因此分享前仍需人工检查。HAR 中未导出 WS 帧时报告 `null`，不会推断为没有实时消息。

详细采样步骤和当前访问证据见 [WPK 接口核查](../../docs/wpk-network-discovery.md)。真实抓包放 `captures/` 或 `artifacts/`，由 Git 忽略；测试使用合成数据，不提交账号会话。
