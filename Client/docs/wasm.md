# Rust / WebAssembly 实现边界（0.3，ABI 3）

当前采用 Rust + serde/serde_json 的纯计算核心，通过最小 C ABI 在 MV3 service worker 和 Node 参考接收器中执行；没有第二套 JS reducer，也没有引入 wasm-bindgen 胶水生成链。

## 已实现职责

- 网络消息边界、连续 u64 序号、ACK 范围和检查加法预算。
- WPK 牌码转换、可见牌集合校验、玩家/金额/动作字段校验。
- 新手边界与发牌前费用、动作语义去重/冲突/补充、当前行动者和阶段。
- 结果通知与最终牌谱双向配对、跨手延迟、本人牌/公共牌/净积分交叉核验、独立积分池守恒。
- 严格业务事件白名单，用于 IDB 写入与参考后端接收的同一份校验。
- 可序列化的脱敏检查点和确定性观察归约。

浏览器专属能力留在 TypeScript：debugger、页面私有对象读取、别名生成、IDB 事务、Web Crypto、WebSocket、计时器和 React。没有复制原生 Web Crypto 到 WASM；没有在 WASM 中训练或实现尚未覆盖的规则引擎。

原始加密 WS 的 Protobuf/AES 解码未移植；页面桥接读取客户端已经解码的数据。WASM 不绕过站点加密，不获得 Chrome 权限，不使 service worker 永不停止。

## ABI

保留 `validate_cards`、`assess_sequence`、`validate_frame`、`validate_ack`、`fits_budget`，`abi_version()` 当前返回 3。host 严格匹配版本。

JSON 接口：

1. JS 序列化 UTF-8，并检查输入不超过 256 KiB。
2. 调用 `prepare_input(len)` 获得 WASM32 指针，向当前 memory.buffer 的已初始化范围复制 len bytes。
3. `process_wpk()` 返回输出字节数，`output_ptr()` 返回输出地址；内存增长后须重新读取 memory.buffer。
4. JS 解码 JSON，检查 ok/error，finally 调用 `clear_buffers()` 清除输入/输出缓冲。

输入 op=reduce 时包含白名单 message 和可选 checkpoint；成功返回 events 与下一 checkpoint。op=validate 接受完整 business_event，仅返回验证结果。serde deny_unknown_fields 拒绝每层未允许字段；递归/输入大小和集合数量受限；错误通过 Result 返回而非 JS 同逻辑回退。

状态只在 IDB 提交后替换。WASM 没有跨调用的业务状态事实源；全局缓冲仅用于一次同步调用，不能充当持久存储。JS 宿主不允许并发重入这一缓冲接口。u64 使用 BigInt，禁止经过 JS Number。

## 打包

`npm run build` 先编译 wasm32 release，再用 esbuild 打包 TS/React；WASM、JS 和许可声明随扩展本地发布。CSP 使用 `script-src 'self' 'wasm-unsafe-eval'`，不启用 unsafe-eval，不执行网络下载的 WASM。无需 WASI、共享内存、多线程或 COOP/COEP。

第三方 Rust 许可通过 Cargo 离线 metadata 和已下载 crate 的 LICENSE 文件收集到扩展 THIRD_PARTY_RUST_NOTICES.txt；不从运行时页面拉取代码。

## 验证范围

原生 Rust 测试覆盖牌码的 52 张唯一映射和基础边界；Node 加载真实 WASM 测试完整业务归约、结算、隐私和错误；Chromium 真实 MV3 验证 CSP、Runtime 桥接、持久检查点、双流 ACK 与重启恢复。最新真实 WPK 尚未现场复测，具体见 validation.md。

官方依据：[扩展 CSP](https://developer.chrome.com/docs/extensions/reference/manifest/content-security-policy)、[MV3 远程代码约束](https://developer.chrome.com/docs/extensions/develop/migrate/remote-hosted-code)、[CDP 调试宿主](https://developer.chrome.com/docs/extensions/reference/api/debugger/)。
