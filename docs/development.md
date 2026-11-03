# 开发

所有本机源码操作在 WSL2 Ubuntu 24.04 的 Linux 文件系统执行。要求 Python 3.12、Node 24、Rust 1.90（含 wasm32-unknown-unknown）、uv 0.13.6。

```bash
uv sync --frozen
npm ci
npm --prefix Client ci
uv run texas-analysis
```

另开终端运行 `npm run dev`，访问本地开发工作台。正式插件使用 `npm --prefix Client run build`，在 Chrome 扩展页加载 `Client/dist`。

- `Client/`：唯一正式 Chrome 客户端。
- `apps/workbench/`：本地开发工作台，不作为 Chrome 扩展交付。
- `services/analysis/`：API、采集接入、推荐、后台学习和自动训练。
- `packages/game-core/`：扑克规则与蘑菇账本。
- `engines/strategy/`：策略抽象、Pluribus MCCFR、搜索与存储。
- `packages/protocol/`：工作台接口类型与生成 schema。
- `training/build_card_buckets.py`：可复现手牌分桶构建。
- `deploy/`：构建、安装和迁移验证。

修改后运行受影响模块的检查；发布前由 CI 运行完整检查并生成两个程序包：

```bash
uv run ruff check .
uv run pytest
npm run check
npm --prefix Client run check
npm --prefix Client run test:python
npm --prefix Client run test:python:browser
bash deploy/build-release.sh
```

CI 在 push、pull request 和手动触发时执行。下载 Actions 中的 Serve、Client 工件。源码包不包含模型或牌局数据。
