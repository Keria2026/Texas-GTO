# 验证记录

## 0.5 云环境对接

合并用户 `2e72f34` 与既有 dev 的 WSS、v1→v2 迁移和关闭连接帧保护。当前验证包含实际页面观察器/WASM → IDB → Python Uvicorn → SQLite → capture.state；两个跨仓库测试覆盖网络与业务丢 ACK、多批次、重传、服务重启、迟到结算和与前端检查点逐字段一致。新增街道/换手完整快照、严格服务器状态响应与陈旧结果拒绝测试。

本次通过：前端 Node 41 项、Rust 8 项、原型离线 5 项、跨仓库 Python 2 项；后端 349 项回归与 npm check；TypeScript、build、Rust fmt/clippy、Ruff 和 12 份冻结来源哈希检查。

当前云环境的系统 Chromium 管理员策略禁止未打包扩展，Playwright 下载受网络策略限制，因此本次未重新通过浏览器套件。下面的 WSL Chromium 结果由用户原提交保留，不能当作本次合并后的浏览器验证。

## 0.3 原环境记录（保留）

日期：2026-11-01。源码位于 WSL Linux 原生 `/home/ubuntu/Project/TGTO-FronEnd`，由原 /tmp checkout 移入。Node 24.22.1、Rust 1.90.0、Chromium 145.0.7632.6。

| 检查 | 结果 |
| --- | --- |
| cargo fmt / clippy（warnings 为错误） | 通过 |
| 原生 Rust 测试 | 8 项通过 |
| TypeScript 严格类型检查 | 通过 |
| Node / 实际 WASM / IDB / WS 测试 | 25 项通过 |
| 原离线结算解析器的合成测试 | 5 项通过 |
| 真实 Chromium MV3 测试 | 2 项通过 |
| 320 / 480 px 侧栏、溢出与错误状态 | 通过 |
| 冻结原型 SHA256、活动文档链接、git diff --check | 通过 |

## 新增业务证据

合成 Cocos/WPK 客户端按真实字段形状提供模块与通知，主 frame 默认上下文经 Runtime binding 将 DTO 交给扩展。完整链路经实际 WASM、IDB 事务及 WS 参考接收器执行，验证 15 条确认动作（含发牌前费用）、本人牌、flop/turn/river、跟注/加注/弃牌、跨手延迟最终牌谱，以及配对后净积分和独立池守恒。示例为虚构三人牌局，不是真实账号资料。

测试中的对手 handCards getter 一旦读取就抛错；原 ID、昵称、认证标记和对手牌不进入 DTO、检查点或后端事件。验证原客户端函数恰好调用一次且返回/异常保持，异步本人牌通过可见状态轮询更新。

故障测试覆盖：错误上下文拒绝、未知字段/动作、非法牌码/重复牌/UTF-8、非安全整数、重复和冲突动作、缺失结算配对、未知金额保持 null、积分差异标 unresolved、重复最终结果冲突、部分事务写入失败后事件和检查点全部回滚。双流独立 ACK，轮换发送；网络-only 后端保留业务 outbox 且不阻塞网络流。

浏览器测试还覆盖侧栏关闭后继续采集、离线积压、同 profile 浏览器重启、业务检查点/结算事件恢复、令牌重新输入、两源补传，以及页面导航中断。Chromium 退出可能先保存 debugger detach 或在启动追加 worker_restart，均要求明确的 interrupted 与持久 gap，不能仅用附着初始缺口充当恢复证明。

## 未覆盖项

没有重新登录真实 WPK 或操作用户 Windows Edge；旧会话字段依据并非最新客户端兼容认证。真实 SharedWorker/OOP iframe 业务适配、双公共牌、极端吞吐、硬件故障、公网多用户和目标策略覆盖尚未验证/实现。双公共牌被明确拒绝，不混成单牌面。原提交时仅参考接收器支持业务；0.5 已完成 Python 双流接入及状态回传，目标玩法策略仍未覆盖。

已有旧 IDB 不删除、不换键；旧源默认 network，新业务源增加字段。检查点仅保留最近 8 手完成状态，事件档案保留；存储有界，达到阈值停止，不承诺无限无人值守采集。真实原始载荷仍只在显式开启时本地保存，不自动上传。

完整命令为 npm run check（本机额外指定临时 sysroot、Cargo 缓存和已安装 Chromium）；CI 使用常规工具链独立运行，结果以提交后的 workflow 为准。
