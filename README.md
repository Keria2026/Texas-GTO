# Texas GTO

以 **Pluribus 的自博弈 blueprint＋深度受限搜索**为核心的扑克研究项目，提供 Chrome 侧栏、本地研究服务、可重放模拟器与算法命令行。

当前版本可以在**没有真实网站数据**的情况下完成模拟牌局、CFR/MCCFR 训练、策略保存与续训、全范围搜索、独立最佳响应评测及牌力计算。支持 2–6 人标准德州规则；策略质量首先在 Kuhn／Leduc 有限博弈验证。完整六人德州仍处于稀疏策略和抽象实验阶段，尚无可靠的大规模已训练策略库，也未复现 Pluribus 论文性能。

用户最新目标为 **8 人、2/4 分盲注、每手强制 8 分 straddle、初始买入 800 分且跨手延续筹码、无抽水，并含桌上筹码缴纳的蘑菇池**。蘑菇费每手发牌前扣，少于 4 人结束结算。当前运行版本尚不支持该完整配置；已确认条件、旧截图差异与待确认项见 [目标规则](docs/mushroom-rules.md)。

## 运行离线工作台

需要 Node.js 24、npm 11/12、Python 3.12 和 uv。依赖用 `package-lock.json`、`uv.lock` 固定。

```sh
npm ci
uv sync --frozen
uv run --frozen texas-analysis
```

服务仅监听 `127.0.0.1:8765`。首次启动在 `artifacts/local/session.token` 写入随机本地令牌（权限 `0600`，每次启动重新生成），日志只显示路径。自行在本机读取该文件，将内容粘贴到工作台的令牌框；界面仅在内存保留令牌。也可预设 `TEXAS_API_TOKEN`，至少 24 个无空白 ASCII 字符；不要提交令牌。

另一个终端启动界面：

```sh
npm run dev
```

打开 `http://127.0.0.1:5173`，检查连接，然后：

1. 在「牌局」创建双人 Kuhn 模拟手牌。
2. 在「训练与策略」训练 1,000 轮 CFR，查看独立评测与实际完成预算。
3. 选择训练产生的 blueprint，查询当前节点的基线频率，或启用有限深度搜索。
4. 回到牌局执行合法动作；旧建议随状态版本变化清除。
5. 「牌力」查看当前牌型、nuts 与 equity；「玩家」查看模拟手牌的公开行动统计。
6. 在策略区的画像适应实验中，比较明确假设风格下的增益和最坏情况损失。

默认示例是合成数据，模拟器可轮流查看当前行动座位；没有连接 WPK、自动下注或读取外部未公开底牌。频率不是胜率或行动 EV；未知信息集返回不支持。牌局事件持久化到 SQLite，支持幂等动作、重启回放和删除。

## 算法命令行

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

在 Chrome 116+ 的 `chrome://extensions` 开启开发者模式，加载 `apps/extension/dist`，点击扩展图标打开侧栏。读取扩展 ID 后，以精确 Origin 启动服务：

```sh
TEXAS_ALLOWED_ORIGINS='http://127.0.0.1:5173,chrome-extension://YOUR_EXTENSION_ID' uv run --frozen texas-analysis
```

不接受通配 Origin。`.env.example` 是配置示例，服务不自动读取 `.env`。扩展仅申请侧栏与本机 HTTP 访问权限；Chrome host 匹配模式不能限制端口，应用代码固定访问 `127.0.0.1:8765`。

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
| `apps/extension` | 模拟、训练、搜索、equity 和画像的侧栏工作台 |
| `packages/protocol` | Zod 运行时契约与生成的 JSON Schema |
| `packages/game-core` | Kuhn／Leduc／无限注德州、牌型和 equity、蘑菇账本 |
| `engines/strategy` | CFR/MCCFR、blueprint、搜索、独立评测、画像与实验适应 |
| `services/analysis` | SQLite 事件、训练队列、模型注册、本地受限 API |
| `adapters/wpk` | HAR 结构检查；真实平台协议尚未接入 |

- [软件结构与 API](docs/software-architecture.md)
- [核心算法设计与后续里程碑](docs/decision-engine.md)
- [Pluribus 阅读笔记](docs/pluribus-paper.md)、[论文原文](docs/papers/README.md)
- [对手建模研究](docs/opponent-modeling-paper.md)、[蘑菇规则缺口](docs/mushroom-rules.md)
- [WPK 接入证据](docs/wpk-network-discovery.md)

画像是 Pluribus 基线之外的项目扩展；蘑菇模块目前验证会话账本，尚未训练跨手策略。真实数据适配、大规模有损抽象／剪枝和长期训练、多人策略强度验证仍需继续完成。

原创代码采用 [MIT](LICENSE)。外部论文、设计文档与资料的来源和权利说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。根目录 [DESIGN.md](DESIGN.md) 保留用户指定的视觉参考原文。
