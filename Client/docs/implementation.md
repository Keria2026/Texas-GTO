# 实施阶段与验收

0.3 已完成本文的网络与初版业务链路：页面桥接、WASM reducer、IDB 检查点与事件同事务、双流能力协商和参考接收器。实际交付范围见 [wpk-business.md](wpk-business.md)；真实网站复测、公网多用户和策略覆盖仍未完成。

0.2 进展：已完成网络采集、WASM 边界检查、IDB 队列、WebSocket 通信、侧栏和协议参考接收器。下列完整业务适配/规则覆盖目标仍按阶段验收，当前运行说明见 [runtime.md](runtime.md)。

## 本次交付范围

设计阶段已完成总体设计与 Rust 核心。0.2 另已交付 MV3 可加载包及网络事件参考接收端点，业务适配和原研究后端改造尚未实现。

本次修改涉及 README、docs、design/manifest.example.json、Rust workspace、Node WASM 测试及 CI；无现有调用者或产品数据迁移。原许可证与忽略文件保留。测试和代码不得包含真实捕获或凭据。

## 第一阶段：可加载扩展与逐条采集

- Vite/React 侧栏和 module service worker；补充 Chrome 类型和最小构建依赖，不引入第二套前端状态框架。
- 用户选 tab、开始/停止、CDP attach、目标/子 session 管理、双向网络消息、退出和 detach 状态。
- 打包本地 WASM、检查 ABI 与 CSP，合成 WS 服务产生已知序列。
- 在 Chrome 与 Edge 分别验证主页面、iframe、Dedicated/Shared Worker、连接重建；比较发送/接收消息计数和内容。测试覆盖范围必须如实列出，不以一个 tab 的成功代表所有目标。

完成标志：关闭侧栏仍记录消息；手动停止不自动恢复；打开 DevTools/取消 debugger/导航导致 detach 时有明确状态和缺口，无静默丢失声明。

## 第二阶段：持久事件与 WASM reducer

- 实现白名单 WPK MAIN world 适配、通用 DTO，Rust 归一化与确定性 reducer。
- IDB 原子写入 event/outbox/checkpoint；状态恢复及配额控制。
- 输入错误、WASM trap、重复源 ID 不同内容、超大消息和溢出均可见；回放相同输入得到相同状态。
- 业务入口变更时 unsupported；不读取未公开对手牌，不采集认证字段。

完成标志：强制停止 worker、在事务前后模拟崩溃、重启浏览器、关闭侧栏均不丢已提交记录；未观察时段有 gap，不能伪造连续历史。磁盘写满时停止并恢复已提交数据，不删除未确认记录。

## 第三阶段：后端交付与实验结果

- 在后端实现/验证 `capture_events_v1` 能力、capture.append/ack、事务幂等与精确 Origin。
- 扩展按序补传、ACK 校验、认证失效、重连退避；新事件流与旧观察快照序号分离。
- 研究控制命令与采集 outbox 分离；结果带状态、规则和模型版本。

完成标志：离线积压后重连、ACK 丢失重发、发送后 worker 被杀、重复 ACK、伪造超前 ACK、服务端冲突/缺序全部验证；旧结果不会显示为当前结果，旧后端不被误报为支持逐条事件。

## 第四阶段：完整对局与发布

- 授权真实会话覆盖发牌、公共牌、check/call/bet/raise/fold/all_in、费用、结算、关闭房间与最终对账。
- 导出匿名事件可重放；事件档案、业务状态和结算口径交叉核对，未解释差额保持 unresolved。
- 输出可加载扩展 ZIP、版本号、构建哈希、权限清单、已验证 Chrome/Edge 版本和已知限制。

完成标志：真实会话和故障注入证据齐全后才称“初版采集器完成”。策略覆盖单独验收，采集成功不等于目标八人规则策略已实现。

## CI

初始 CI 仅验证已存在内容：cargo fmt / clippy / 原生测试 / wasm32 release / Node 实际 WASM 加载测试。后续出现扩展代码后再加入 TS 检查、Vite 构建、合成浏览器采集与 IDB 恢复测试，不能用空脚本标成全链路通过。
