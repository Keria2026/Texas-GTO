# WPK 适配器研究入口

目标站点：https://h5.wpk.com/ 。当前仅实现 **本地 HAR 结构分析器**，尚无已确认的 WPK API、消息解码或牌局事件映射。

```sh
uv run --frozen texas-wpk-inspect /path/to/wpk.har --out artifacts/wpk/endpoint-report.json
```

命令只读取本地文件，不发送网络请求。忽略请求头、Cookie、请求正文、URL 查询值与响应字段值；报告保留经过有限过滤的路由、字段路径与类型，因此分享前仍需人工检查。HAR 中未导出 WS 帧时报告 `null`，不会推断为没有实时消息。

详细采样步骤和当前访问证据见 [WPK 接口核查](../../docs/wpk-network-discovery.md)。真实抓包放 `captures/` 或 `artifacts/`，由 Git 忽略；测试使用合成数据，不提交账号会话。
