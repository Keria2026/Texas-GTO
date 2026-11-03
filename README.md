# Texas GTO

正式 Chrome 扩展现位于 [Client/](Client/)，与算法／训练服务共用本仓库。GitHub Actions 对每次 push（包括合并后的提交）和 pull request 执行检查与打包，生成 **Serve** 和 **Client** 两个下载工件。Serve 包含 Ubuntu 24.04 的离线安装器与锁定依赖，Client 为可加载扩展。安装和数据迁移见 [Serve 说明](deploy/README-Serve.md)、[Client 说明](deploy/README-Client.md)。

已完成能力、V1.0 剩余开发项、优先级及发布验收条件统一见 [V1.0 Roadmap](ROADMAP.md)。当前缺口包含算法、规则、真实样本学习、画像决策和质量验收，不能仅靠增加训练轮数解决。

当前交付为 **Pr1.0（扩展 0.8.2）**：从当前下注轮起点重新求解，已发布的玩家条件行为模型参与八人搜索，并以独立后续收益比较决定是否调整基线。真实样本自动经过回放校验、整手时间留出评测、版本发布或拒绝，支持回滚和迁移。功能与门槛见 [Pr1.0 说明](docs/pr1.0.md)。模型仍是两手视野、有限抽象和限时近似；覆盖、长期收益、4–7 人、re-straddle 与真实八人质量验收见 roadmap，不能把 Pr1.0 当作完整 V1.0 验收。

以 **Pluribus 的自博弈 blueprint＋深度受限搜索**为核心的扑克研究项目，目标产品为 **Chrome 扩展实时采集 WPK 数据＋服务器承担算法计算、搜索与训练**。当前提供可运行的离线研究工作台、模拟器与算法命令行。

当前版本可以在**没有真实网站数据**的情况下完成模拟牌局、CFR/MCCFR 训练、策略保存与续训、全范围搜索、独立最佳响应评测及牌力计算。支持 2–8 人标准德州规则，4–8 人可配置足额 live straddle，筹码可逐席指定；策略质量首先在 Kuhn／Leduc 有限博弈验证。完整德州仍处于稀疏策略和抽象实验阶段，尚无可靠的大规模已训练策略库，也未复现 Pluribus 论文性能。

用户最新目标为 **4–8 人、以 8 人为主，2/4 分盲注、每手强制 8 分 straddle、初始买入 800 分且跨手延续筹码、无抽水，并含桌上筹码缴纳的蘑菇池**。蘑菇费每手发牌前扣，少于 4 人结束结算。当前运行版本尚不支持该完整配置；已确认条件、旧截图差异与待确认项见 [目标规则](docs/mushroom-rules.md)。

生产用户安装 Chrome 扩展后连接服务器使用，无需在本机运行 Python。目标职责、实时传输与待迁移项见 [扩展与服务器架构](docs/extension-server-architecture.md)。下面的本地启动命令供开发与算法验证；WPK 业务采集与 Python 接入已完成合成链路验证，见 [扩展对接](docs/tgto-integration.md)；真实牌桌验收和生产服务器部署尚未完成。

## 运行离线工作台（开发用途）

需要 Node.js 24、npm 11/12、Python 3.12 和 uv。依赖用 `package-lock.json`、`uv.lock` 固定。

```sh
npm ci
uv sync --frozen
uv run --frozen texas-analysis
```

服务仅监听 `127.0.0.1:8765`，默认本地模式无需令牌。打开工作台或加载 TGTO 采集扩展后自动连接，服务恢复后自动重试。普通网页来源与伪造 Host 被拒绝；HTTP 客户端自动发送固定 `X-Texas-Local: 1` 标记，WebSocket 接受合法 Chrome 扩展 Origin。默认不再生成或读取 `session.token`。

本机已配置 Windows 登录后启动 Ubuntu-24.04，再由 systemd 用户服务托管算法端，进程退出后自动重启；无需重复运行上述服务命令。安装、日志与停用方法见 [WSL 本地服务自启动](docs/local-startup.md)。工作台的 Vite 开发服务器仍按需启动。

另一个终端启动界面：

```sh
npm run dev
```

打开 `http://127.0.0.1:5173`，界面自动连接服务：

1. “核心策略自动训练”显示同一套蘑菇策略库的实际进度，跨 489 个公开开局自博弈，当前使用固定 4 分前注。服务空闲 30 秒后自动训练，完成迭代保存同一份检查点，重启自动续训；历史总轮数与当前规则轮数分别显示。分桶和工件升级边界见[上下文说明](docs/mushroom-context.md)与[前注接入](docs/ante-migration.md)。
2. 收到牌局事件／计算请求时让位；关闭页面不停止服务端训练。当前两手视野为近似，仅在输入和策略覆盖通过时用于实局推荐。
3. 算法开发时可展开“算法验证（开发工具）”运行 Kuhn／Leduc，或在“算法验证策略库”检查对应工件。
4. 需要时展开“画像适应实验”，比较假设对手模型下的收益与最坏情况损失。资源上限与导入导出见 [训练说明](training/README.md)。

前端已按用户要求移除牌局模拟与牌力计算，以及依赖模拟牌局的节点查询和模拟画像操作。服务器的规则、牌力、搜索和模拟 API 保留用于算法验证。Kuhn 支持 2–6 人、Leduc 固定 2 人，它们不代表目标德州·蘑菇训练；蘑菇有限视野训练已接入，完整长期策略与 WPK 实时推荐尚未完成。

策略库包含同一训练任务各信息集的行动分布。信息集数量是保存的决策情景数量，不是独立模型数或样本数；不同情景通常有不同策略，同一工件的同一信息集查询保持一致。

## 算法命令行

新增 `--algorithm pluribus`：按作者补充材料实现 on-policy external-sampling MCCFR、负遗憾剪枝、早期折扣、翻前策略采样与翻后快照。网页训练默认使用此算法。调度使用可复现的完成迭代数，区别于原论文的运行分钟；参数、验证和尚缺部分见 [Pluribus 实现](docs/pluribus-implementation.md)。

```sh
uv run --frozen texas-research train \
  --game kuhn --players 2 --algorithm cfr --weighting linear \
  --iterations 3000 --seed 20261031 \
  --blueprint artifacts/kuhn.json --checkpoint checkpoints/kuhn.json

uv run --frozen texas-research evaluate \
  --game kuhn --players 2 --blueprint artifacts/kuhn.json \
  --mode exact --require-coverage

uv run --frozen texas-research resume \
  --game kuhn --players 2 --from-checkpoint checkpoints/kuhn.json \
  --iterations 1000 --blueprint artifacts/kuhn-next.json
```

[离线算法运行说明](docs/offline-algorithms.md)记录完整命令、画像／蘑菇模块用法和[实际实验数值](docs/experiments/offline-baseline.json)。固定种子的双人 Kuhn 3,000 轮实验收益接近理论 `-1/18`；多人及完整德州不据此声称纳什收敛或最优。CLI 可用于服务器 CPU 作业；本地 API 只接受小型有预算任务。

## 加载 Chrome 扩展

```sh
npm run build
```

在 Chrome 116+ 的 `chrome://extensions` 开启开发者模式，加载 `apps/extension/dist`，点击扩展图标打开侧栏即可自动连接本地算法端。正式 WPK 采集扩展使用相邻 `TGTO-FronEnd/dist`，安装或浏览器重启后由后台自动连接；开始采集仍需用户主动操作。

默认允许两个工作台端口 5173/5174 和格式合法的 Chrome 扩展 Origin，不接受任意网页 Origin 或通配字符串。仅本地默认模式免令牌；显式设置 `TEXAS_API_TOKEN` 会进入原令牌模式，用于独立测试或远端服务，并需明确配置扩展 Origin。`.env.example` 不会自动加载。

## 检查

```sh
npm run check
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest
```

协议修改后运行 `npm run schema:generate`。检查覆盖规则守恒、牌型黄金样例及独立对照、信息集一致性、CFR 已知均衡与独立最佳响应、checkpoint 续训、公共根／隐蔽后续选择、画像时间边界、蘑菇账本、服务鉴权与事件重放，以及 TypeScript／JSON Schema／扩展构建。完整枚举五张牌分类的额外审计命令见算法文档。

## 模块与资料

| 路径 | 当前职责 |
| --- | --- |
| `apps/extension` | 算法验证训练、策略库管理与假设画像适应实验 |
| `packages/protocol` | Zod 运行时契约与生成的 JSON Schema |
| `packages/game-core` | Kuhn／Leduc／无限注德州、牌型和 equity、蘑菇账本 |
| `engines/strategy` | CFR/MCCFR、blueprint、搜索、独立评测、画像与实验适应 |
| `services/analysis` | SQLite 事件、训练队列、模型注册、本地受限 API |
| `adapters/wpk` | HAR 结构检查；真实平台协议尚未接入 |

- [TGTO 扩展与 Python 后端实际对接、WSS 配置](docs/tgto-integration.md)
- [TGTO-FronEnd 实时采集与通信源码核查](docs/tgto-frontend-review.md)
- [软件结构与 API](docs/software-architecture.md)
- [核心算法设计与后续里程碑](docs/decision-engine.md)
- [Pluribus 阅读笔记](docs/pluribus-paper.md)、[论文原文](docs/papers/README.md)
- [对手建模研究](docs/opponent-modeling-paper.md)、[蘑菇规则缺口](docs/mushroom-rules.md)
- [WPK 接入证据](docs/wpk-network-discovery.md)

画像是 Pluribus 基线之外的项目扩展；蘑菇模块已接入有限视野跨手训练及自动续训。真实数据匹配、生产抽象与长期价值、多人策略强度验证仍需继续完成，自动候选不自动晋级为实局推荐模型。

原创代码采用 [MIT](LICENSE)。外部论文、设计文档与资料的来源和权利说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。根目录 [DESIGN.md](DESIGN.md) 保留用户指定的视觉参考原文。
