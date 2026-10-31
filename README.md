# Texas GTO

以 **Pluribus 自博弈与实时搜索**为核心的德州扑克研究项目，面向 Chrome 侧栏、离线复盘、玩家画像和蘑菇玩法扩展。

当前为 `0.1.0` 基础骨架：可构建的 Manifest V3 侧栏、可启动的 FastAPI 健康接口、共享协议、游戏接口与遗憾匹配基础函数。尚未实现 CFR 遍历、训练 blueprint、实时求解、牌谱导入或 WPK 采集，当前不产生下注建议。

## 开发环境

需要 Node.js 24、npm 11/12、Python 3.12 和 uv。依赖由 `package-lock.json` 与 `uv.lock` 固定。

```sh
npm ci
uv sync --frozen
```

终端一启动本地服务（仅监听 `127.0.0.1:8765`）：

```sh
uv run --frozen texas-analysis
```

终端二启动浏览器预览：

```sh
npm run dev
```

打开 `http://127.0.0.1:5173`，点击“检查连接”。当前唯一 API 为公开的 `GET /v1/health`，只返回服务和未实现的策略能力信息，不传输牌谱或玩家数据。

## 加载 Chrome 扩展

```sh
npm run build
```

在 Chrome 116+ 的 `chrome://extensions` 开启开发者模式，加载已解压目录 `apps/extension/dist`，点击扩展图标打开侧栏。读取扩展 ID 后，用精确 Origin 重新启动服务：

```sh
TEXAS_ALLOWED_ORIGINS='http://127.0.0.1:5173,chrome-extension://YOUR_EXTENSION_ID' uv run --frozen texas-analysis
```

将 `YOUR_EXTENSION_ID` 替换成实际 ID。不接受通配 Origin。`.env.example` 只提供配置示例，服务不自动读取 `.env`。扩展目前仅申请侧栏与本机 HTTP 访问权限；Chrome 匹配模式不支持按端口限制 host 权限，应用代码固定访问 `127.0.0.1:8765`。没有 content script 或自动采集。

## 检查

```sh
npm run check
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest
```

协议更新后运行 `npm run schema:generate`。CI 检查 TypeScript、协议拒识、扩展构建、Python 格式／静态检查、API Origin 边界、跨语言响应契约和遗憾匹配。完整 CFR 收敛与 Chrome 实际运行验收属于后续工作。

## 目录

```text
apps/extension/          React + TypeScript 侧栏及 MV3 入口
packages/protocol/       Zod 契约、生成的 JSON Schema、协议测试
packages/game-core/      Python 不完全信息博弈接口
engines/strategy/        Pluribus 方法的接口与遗憾匹配起点
services/analysis/       FastAPI 本地服务与接口测试
adapters/wpk/           本地 HAR 结构分析；真实 WPK 协议尚未接入
training/               自博弈作业的开发入口说明
fixtures/protocol/      跨语言共用的合成协议样例
docs/papers/            用户上传的两篇论文原文及校验和
docs/requirements/      原始需求文档快照
docs/                   核心算法、软件架构、规则和研究记录
DESIGN.md               用户指定的视觉设计参考原文
AGENTS.md               项目开发约束与算法优先级
```

## 设计与研究

- [核心算法与 A0–A5 里程碑](docs/decision-engine.md)
- [软件实现结构与当前接口](docs/software-architecture.md)
- [WPK 接口采样、访问证据与本地分析工具](docs/wpk-network-discovery.md)
- [Pluribus 原文笔记](docs/pluribus-paper.md)及[论文原文目录](docs/papers/README.md)
- [玩家画像与专家适应](docs/opponent-modeling-paper.md)
- [蘑菇规则及未确认边界](docs/mushroom-rules.md)
- [视觉设计参考的来源和应用范围](docs/design-source.md)

先验证关闭画像的“自博弈 → blueprint → 搜索 → 独立评测”，再扩展标准六人德州、画像适应和蘑菇会话收益。TexasSolver 用于局部对照。基础函数与接口骨架不代表已复现 Pluribus。

项目原创代码采用 [MIT](LICENSE)。论文、外部设计文档及原始参考资料的来源和权利说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)，不因归档而改变其原有权利。
