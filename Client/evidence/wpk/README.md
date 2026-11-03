# WPK 原型来源与字段证据

此前文档所指的原型现已实际纳入本仓库，可直接浏览，不再依赖其他会话的临时目录。

- 原仓库：Keria2026/Texas-GTO，工作分支 dev，基线 `0483bc26b3b065514e81df7e86fbf296d1374f51`。
- 原状态：本机未提交修改，不是远端已有分支。本次按原文件复制到 legacy/；哈希见 [provenance.json](provenance.json)。历史文档的相对链接仍按原仓库布局，不代表本仓库已包含其全部服务。
- [legacy/adapters/wpk/observer.mjs](legacy/adapters/wpk/observer.mjs)：原始 HMFClient / WPGameData 只读观察器。
- [legacy/adapters/wpk/stream.mjs](legacy/adapters/wpk/stream.mjs)：原 CDP 注入桥接；不是当前 MV3 的入口。
- [legacy/packages/protocol/src/index.ts](legacy/packages/protocol/src/index.ts)：原观测契约，依赖旧仓库 zod，不参与本版扩展打包。
- [legacy/packages/protocol/test/live-observer.test.mjs](legacy/packages/protocol/test/live-observer.test.mjs)：旧合成用例。
- [offline/src/offline-hands.mjs](offline/src/offline-hands.mjs)：旧离线配对结算解析器及相邻 test/ 用例。
- [field-shapes.json](field-shapes.json)：此前授权会话结束阶段消息的键和类型；原始值全部移除，不包含账号、牌局数据或会话材料。

本目录是来源依据，不直接加载到网站。当前适配器为 extension/wpk-page.ts + extension/wpk.ts，纯解析与校验位于 crates/capture-core/src/wpk.rs。完整字段表和证据强度见 [业务适配说明](../../docs/wpk-business.md)。

“此前已验证”仅指先前本机会话，不能当作最新 WPK 客户端的稳定公开 API。新实现以合成用例和真实 Chromium 注入链路验证，尚未重新登录真实站点。真实抓包、账号、令牌、密钥和未公开对手牌不在本目录。
