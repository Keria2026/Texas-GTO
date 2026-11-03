# 0.2 网络采集与交付契约

本文件保留网络流约定，0.5 同时安装业务适配器并对接 Python 双流服务，见 [业务适配](wpk-business.md) 和 [业务协议](protocol.md)。前端侧栏不是连接所有者，所有采集和发送位于扩展 service worker。

## 可加载包

`npm run build` 先编译 Rust wasm32，再用 esbuild 打包 TS/React，输出 dist。Manifest 使用 MV3 module worker、Chrome 125+、debugger/storage/sidePanel/alarms 和本机 loopback 权限。WASM 随包提供，CSP 显式允许 wasm-unsafe-eval，不含远程执行代码。

生产包仅允许 `https://h5.wpk.com/` 及其同源 blob 子目标；运行时仍校验目标和端点。已知网络端点为游戏 `wss://game.wpk111.com:11111/wepoker/game4/battle/echo` 和大厅 `wss://a92smtp.depucloud.cn:18002/`。网站变更需要核对后更新白名单，不泛抓未知端点。URL 查询、fragment 和 userinfo 不存档。

测试构建 dist-fixture 单独启用 127.0.0.1:8767 合成页面与 WS；生产代码经编译去除该开关。host_permissions 的 loopback 匹配不能当成端点端口隔离，运行时后端地址和 CSP 另行限制为 8765/8766。

## 网络事件 schema

默认本地 Python 客户端先发送 `{"type":"auth","capabilities":["network_status_v1","capture_state_v1"]}`，免令牌；远端或 Node 参考接收器的显式令牌模式另带 token。服务端成功返回：

```json
{"type":"ready","schema_version":"1.2","capabilities":["network_events_v1"]}
```

网络与 `capture_events_v1` 业务能力独立协商；后端至少支持一类即可连接，未支持的流留在 outbox。

```json
{
  "schema":"tgto.network/0.1",
  "type":"network.append",
  "source_id":"synthetic-source",
  "events":[{
    "schema":"tgto.network/0.1",
    "type":"network_message",
    "source_id":"synthetic-source",
    "sequence":"1",
    "captured_at":"2026-11-01T00:00:00.000Z",
    "data":{
      "connection_id":"synthetic-connection",
      "endpoint":"wss://game.wpk111.com:11111/wepoker/game4/battle/echo",
      "direction":"received",
      "opcode":2,
      "byte_length":3,
      "sha256":"0000000000000000000000000000000000000000000000000000000000000000",
      "monotonic_time":1.25
    }
  }]
}
```

上例摘要为展示占位值，不是某真实载荷的哈希。实际记录由 Web Crypto SHA-256 计算。type 还支持 socket_open/socket_closed/socket_error/capture_gap；这些控制记录也使用连续网络流序号。data 只允许连接、端点、方向、opcode、长度、摘要、单调时间和固定故障原因，不含原始载荷、认证字段或账号资料。

服务端提交后返回：

```json
{"schema":"tgto.network/0.1","type":"network.ack","source_id":"synthetic-source","through_sequence":"1"}
```

序号为规范十进制 u64 字符串，从 1 开始。只允许一个批次在途（最多 64 条、256 KiB）；有效 ACK 持久更新进度并移除 outbox 引用，保留原档案。重复 ACK 无副作用；错误来源、倒退、超前 ACK 或冲突让通信停止并保留未确认数据。连接中断或 ACK 丢失后原序号重发，服务端必须按 source+sequence 和规范化内容幂等提交。后端错误不能由客户端改号跳过。

每 20 秒发送应用层 ping，服务端返回 pong；超过响应期限或握手/ACK 超时关闭连接并重试。普通断线指数退避并加入抖动，最长 60 秒；认证/协议拒绝要求人工修正再连接。本地后台在浏览器重启后自动连接；远端显式令牌模式仍需凭据。

## 持久化与失败语义

数据库名仍为 `tgto-capture-v1`，版本为 2；四个 store：sessions、events、outbox、settings。业务检查点保存在独立业务 session，与该次事件原子提交。旧 v1 档案原位保留；settings 绑定服务器地址，更换地址时从档案重建两类 outbox。

一次 strict readwrite 事务分配序号、存档记录、增加 outbox 引用并更新会话；事务失败完整回滚。session 保存累计写入、确认进度、字节数、raw 标志和活动状态；事件键用 20 位左补零排序。WASM 内存不是持久事实源。

默认不保存 raw，勾选后将 UTF-8 文本或 base64 二进制载荷随本地记录保存；发送时仅选择 record 元数据，原始载荷不会进入 network.append。元数据导出默认也不包含 raw；导出本地载荷需要另行确认。默认诊断范围仅限这个采集会话，无自动轮换删除，停止后仍保留，须由用户管理浏览器存储。

单条网络消息上限 1 MiB（控制消息最多 125 bytes），每个采集会话总存档预算 64 MiB；队列内存最多 128 个事件或约 8 MiB，达到任一限制停止。配额、事务、WASM 或解析失败不静默丢弃，不覆盖未确认记录；持久故障状态另存在 storage.local，仍能读取并导出已有记录。没有实现已确认档案自动清理，不适合无人值守无限期存档。

启动时发现未正常结束的活动会话，将其标记 interrupted、尽力追加 worker_restart gap，并解除旧会话附着；不会自动重新抓取游戏页面。已经落盘的 outbox 可在本地自动重连或远端重新鉴权后继续发送。无法写入 gap 时仍保留不洁会话/独立错误状态，不声称连续覆盖。

## 初始覆盖和目标生命周期

开始时总有 attached_mid_stream gap，附着前历史不可取。只有收到允许端点的 WebSocketCreated 后才接受该连接的数据；原已建立且未映射端点的连接不会绕过过滤。应在开始后进入对局建立新连接，不自动刷新或断开页面。

子目标在初始化网络监听前短暂停止，finally 总会恢复执行；不会通过暂停游戏实现策略控制。根页面导航、debugger 脱离和目标关闭停止采集。已知子目标退出记录缺口。未通过白名单的目标恢复后解除附着；子目标初始化失败导致当前采集停止并报告覆盖中断。

## 参考接收器与既有后端

`tools/reference-backend.ts` 使用 Node 24 node:sqlite 与 ws，只监听 127.0.0.1:8766。精确 Origin、先鉴权、输入白名单、批次限制、SQLite FULL/WAL 事务与提交后 ACK 均实际执行；重复记录核对规范化内容，冲突回滚整批。令牌/数据库目录 0700，令牌和主数据库 0600。它不计算牌力、策略或训练，不是多用户生产后端。

Texas-GTO 的 8765 端点已支持双流和协商的 capture.state；默认本地模式无需配置扩展 ID 或令牌，安装后自动连接。构建时 TGTO_BACKEND_URL 可固定 WSS 地址与权限/CSP。Windows 到 WSL 的 localhost 转发需本机环境支持；当前云环境未完成 Chrome 侧栏实机验收。
