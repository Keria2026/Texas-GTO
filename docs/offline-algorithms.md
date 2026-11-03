# 离线算法：运行、复现和当前边界

当前实现把 Pluribus 的核心结构落到可验证的小型博弈：自博弈训练 blueprint、按公共到达分布构造搜索根、深度受限搜索和各玩家隐蔽选择后续策略。代码还包含标准德州规则模拟、可独立使用的画像后验与蘑菇账本。所有例子均为离线模拟，不依赖真实接口、账号或其他玩家未公开底牌。

这是可执行的研究基础，不是完整 Pluribus 复现。尚未实现其生产规模的牌张／行动抽象、训练优化或六人德州已训练策略库。后文的真实实验显示：小型游戏可以训练和度量偏离收益；完整牌张的德州表格策略覆盖仍很稀疏。

## 已实现的主线

新增 `algorithm=pluribus` 的补充材料训练核心，含 on-policy external sampling、可恢复的负遗憾剪枝、早期周期折扣、翻前动作计数和翻后策略快照。原 `cfr/mccfr` 保留作对照。实现与原文运行时间调度的差异、命令和验证见 [Pluribus 实现](pluribus-implementation.md)。

| 模块 | 当前行为 | 边界 |
| --- | --- | --- |
| `texas_game_core.kuhn` | 2–6 人有限 Kuhn，2 人采用标准规则 | 多人是明确版本的研究扩展，不是德州 |
| `texas_game_core.leduc` | 两人、两轮下注的有限 Leduc，显式加注上限 | 仅这一规则版本 |
| `texas_game_core.holdem` | 2–8 人标准德州、4–8 人足额 live straddle、逐席筹码、合法下注、全下和边池、有限研究动作集 | 无抽水／蘑菇，短 straddle 拒绝，CLI 不做全树枚举 |
| `texas_strategy.cfr.Trainer` | 完整树 CFR、带重要性修正和完整支持采样的 external-sampling MCCFR；uniform 或 linear 迭代权重 | 完美记忆游戏；多人不具有一般纳什收敛保证 |
| `texas_strategy.blueprint` | 有版本和校验和的 JSON blueprint、保留 RNG 状态的 JSON checkpoint | 校验和检测损坏，不证明来源可信；不加载 pickle |
| `texas_strategy.abstraction` | 德州全局花色置换规范化，翻前 1,326 组合映射到 169 类，保留逐街可见牌历史 | 同构合并；不是 Pluribus 的生产规模牌张分桶 |
| `texas_strategy.mushroom_abstraction`、`card_buckets` | 蘑菇局的相对座位、筹码档、当前手行动、翻前 169／每街翻后 200 桶；建议映射回合法实际金额 | 有损、不完全记忆、有限视野近似；尚无实局质量认证，见[上下文模型](mushroom-context.md) |
| `texas_strategy.search` | 小博弈全范围公共根；各活跃玩家隐蔽选择 blueprint／fold／call／raise 后续策略；精确或 MCCFR 抽样尾值 | 八人抽样已验证但需要外部提供公共信念；偏置参数是工程选择；不是安全求解或完整 Pluribus 复现 |
| `texas_strategy.evaluation` | 信息集一致的精确最佳响应、NashConv、固定对手下的成对抽样比较 | 精确枚举有节点预算；超限不返回伪造的完整结果 |

原 `cfr/mccfr` 每次训练迭代固定策略，再更新每位玩家的遗憾和平均策略；新增 `pluribus` 按补充伪代码顺序更新玩家，使用独立的策略积累与周期折扣。预算中断会丢弃未完成迭代并回滚 RNG，使 checkpoint 续训与同种子连续训练一致。CLI 报告实际完成迭代、访问节点、时间和停止原因，不把请求的迭代数冒充完成量。

Blueprint 将规则版本、人数、算法、种子、迭代数和行动支持与策略绑定；原对照算法版本为 `algorithm_version="tabular-cfr-v1"`，新增主线为 `pluribus-iteration-schedule-v1`。`chance_sampling` 区分有限游戏的 `enumerated` 与德州 MCCFR 的精确分布快捷采样 `game_exact`，checkpoint 拒绝跨采样接口续训。未训练信息集可显式使用均匀回退来做研究 rollout；这不等于策略覆盖。`--require-coverage` 会拒绝这种回退。行动频率、行动 EV、策略覆盖率是不同指标。

## 命令行与工件

在仓库根目录、依赖已通过 `uv sync --frozen` 安装后执行。工件放在已忽略的 `artifacts/` 和 `checkpoints/`；报告打印到 stdout，也可用 `--report` 保存。

```bash
uv run --frozen python -m texas_strategy.cli train \
  --game kuhn --players 2 --algorithm cfr --weighting linear \
  --iterations 3000 --seed 20261031 --max-nodes 1000000 --seconds 30 \
  --blueprint artifacts/kuhn-2p.json --checkpoint checkpoints/kuhn-2p.json

uv run --frozen python -m texas_strategy.cli evaluate \
  --game kuhn --players 2 --blueprint artifacts/kuhn-2p.json \
  --mode exact --require-coverage --report artifacts/kuhn-2p-evaluation.json

uv run --frozen python -m texas_strategy.cli resume \
  --game kuhn --players 2 --from-checkpoint checkpoints/kuhn-2p.json \
  --iterations 1000 --blueprint artifacts/kuhn-2p-4000.json \
  --checkpoint checkpoints/kuhn-2p.json
```

`resume --iterations` 表示新增迭代数；算法、迭代权重、种子和 RNG 从 checkpoint 恢复，不能在续训时偷换算法。所有工件都校验当前游戏和规则配置。Blueprint、checkpoint、report 输出不能使用同一路径；report 不能覆盖输入工件。

新 checkpoint 使用紧凑 JSON schema 2，共享动作列表并移除每行重复字段名，完整保留累计值与 RNG；继续支持 schema 1 导入。Blueprint 格式不变。模型规则版本与文件 schema 是两项不同的兼容性检查，换信息集编码不能仅靠换文件格式续训。

另外两个有限游戏实验的复现命令：

```bash
uv run --frozen python -m texas_strategy.cli train \
  --game kuhn --players 3 --algorithm mccfr --weighting linear \
  --iterations 5000 --seed 20261031 --blueprint artifacts/kuhn-3p.json

uv run --frozen python -m texas_strategy.cli evaluate \
  --game kuhn --players 3 --blueprint artifacts/kuhn-3p.json --mode exact

uv run --frozen python -m texas_strategy.cli train \
  --game leduc --players 2 --algorithm mccfr --weighting linear \
  --iterations 3000 --seed 20261031 --blueprint artifacts/leduc-2p.json

uv run --frozen python -m texas_strategy.cli evaluate \
  --game leduc --players 2 --blueprint artifacts/leduc-2p.json --mode exact
```

德州只运行有预算的 MCCFR 和抽样比较。以下是每人 4 个最小筹码单位、大小盲 1/2 的 **2bb 短筹码实验**；`--bet-fractions none` 仍保留规则引擎提供的最小加注和全下，省略额外底池比例档位。

```bash
uv run --frozen python -m texas_strategy.cli train \
  --game holdem --players 6 --stacks 4,4,4,4,4,4 --bet-fractions none \
  --algorithm mccfr --weighting linear --iterations 50 --seed 20261031 \
  --blueprint artifacts/holdem-6p-sparse.json

uv run --frozen python -m texas_strategy.cli evaluate \
  --game holdem --players 6 --stacks 4,4,4,4,4,4 --bet-fractions none \
  --blueprint artifacts/holdem-6p-sparse.json --mode sampled \
  --samples 200 --seed 20261032 --player 0
```

将上述配置改为 `--players 3 --stacks 4,4,4` 可复现三人稀疏实验。在训练和评测两条命令中同时加 `--abstraction suit_canonical`，并使用新的工件路径，可复现花色规范化对照。该模式只合并对自己可见牌的全局花色同构，保留公共行动、筹码和各街可见牌历史，不查看对手私牌。它不是按牌力粗分桶。

Hold'em 的 `--small-blind`、`--big-blind`、`--button`、`--bet-fractions`、`--stacks` 和 `--abstraction` 都进入规则标识；评测和续训必须保持一致。未指定 `--stacks` 时 CLI 使用每人 4 单位的短筹码配置，直接调用 `HoldemGame()` 的默认配置则不同。

`evaluate --mode sampled` 默认比较“均匀策略”与“仅替换指定座位的候选策略”，其余座位固定为均匀策略；`--baseline` 可改用另一份相同规则的 blueprint。每对模拟共享随机流，输出候选相对基线的净筹码差和成对抽样标准误。这不是最坏情况可剥削度，也不能单凭击败均匀对手证明策略质量。可用 `--max-nodes`、`--seconds` 限制所有命令；错误返回非零退出码，预算不足的精确评测不会输出部分树当作精确答案。

## 已执行实验

2026-11-01 增加八人蘑菇的抽样后续搜索，代码入口为 `solve_subgame(..., algorithm="mccfr", sampled_rollouts=True)`，默认仍保留小博弈精确尾值。执行 `uv run --frozen python training/verify_sampled_search.py` 可重跑，当前原始结果见 [sampled-search.json](experiments/sampled-search.json)。该脚本含断言，失败返回非零退出码。

- Kuhn：先以 Pluribus 训练 3,000 轮，再执行 3,000 轮深度 1 的抽样搜索，使用六个完整公共隐藏世界。独立精确求解搜索子博弈的最佳响应，可剥削度为 0.009675，策略值约 -0.054529；无均匀回退。此处数值只属于这个有限子博弈。
- 八人蘑菇：每人 800 分、两手视野、八个**合成**隐藏世界、**合成均匀**后续策略，32 轮约 10.39 秒，完成 1,568 条后续轨迹、57,757 个尾部节点。原精确尾值在 10,000 节点预算内无法完成首轮。两个预算不同，不能据此计算加速倍数；这些是一次本机测量，不是 p95 或实战收益。
- 轨迹样本不作为永久叶值缓存；固定后续策略的一次分布查询只在抽样和动作验证之间复用，最多保留一个状态，不复用随机收益。少于四人的提前结束、下一手续局与净积分守恒纳入测试。

抽样减少了未来发牌／行动的枚举成本，但不自动提供可信根分布、成熟 blueprint 或搜索收敛。缺少后续策略覆盖仍拒绝求解；预算未训练到请求的信息集时仍返回不支持，不填充均匀建议。真实 WPK 重建、八人全范围与实时延迟验收仍待完成。

完整数值、规则标识、工件校验和、阶段预算与实际耗时见 [offline-baseline.json](experiments/offline-baseline.json)。2026-10-31 在当前云环境运行，训练种子 `20261031`，uniform-fallback 基线也纳入评测。各阶段均完成请求迭代，没有时间或节点预算截断。训练工件留在 `/tmp`，仓库仅保存小型汇总。

最终元数据更新后，已重新生成全部工件并审计文档命令：7 组实验的 CLI 一次性训练与分阶段 API 训练得到完全相同的 blueprint checksum，CLI 评测数值及覆盖计数也一致；5 组适应实验除墙钟耗时外逐字段一致。另验证 checkpoint 从 3,000 次新增 1,000 次成为 4,000 次。审计只将输出目录替换为独立的 `/tmp` 路径，实际执行 `uv run --frozen python -m texas_strategy.cli`；相关源码 SHA-256、各工件完整 metadata 和审计时间保存在汇总 JSON 的 `source_sha256`、`artifact_metadata`、`audit` 字段。耗时不要求逐次一致。

| 游戏／算法 | 迭代数 | 初始 NashConv | 最终 NashConv | seat 0 自博弈价值 | 评测查询覆盖 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Kuhn 2 人／Linear CFR | 3,000 | 0.916667 | 0.009073 | -0.055607 | 100% |
| Kuhn 3 人／Linear MCCFR | 5,000 | 2.062500 | 0.088406 | -0.022271 | 100% |
| Leduc 2 人／Linear MCCFR | 3,000 | 4.747222 | 0.953280 | -0.085582 | 97.88% |

两人 Kuhn 的理论均衡价值为 seat 0 的 `-1/18 ≈ -0.055556`。本次 100／1,000／3,000 次的 NashConv 分别为 `0.051892 / 0.016705 / 0.009073`，支持这条有限游戏实现的收敛检查。三人 Kuhn 在 1,000／5,000 次为 `0.089136 / 0.088406`，只能说明这次运行的偏离收益；不能推出多人算法一般收敛。Leduc 的最终偏离收益仍大，不应当作充分训练的基线。

两人零和时报告 `exploitability = NashConv / 2`；多人保留 NashConv，`exploitability` 为 `null`。上述价值单位是游戏的净筹码单位，不是胜率。

| 德州稀疏实验 | MCCFR 迭代 | 成对样本数 | 候选 seat 0 相对均匀基线增益 | 增益标准误 | 候选评测查询覆盖 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 3 人、原始牌张、每人 4 单位 | 50 | 200 | 0 | 0 | 3.01% |
| 3 人、花色规范化、每人 4 单位 | 50 | 200 | 0 | 0 | 22.18% |
| 6 人、原始牌张、每人 4 单位 | 50 | 200 | 0 | 0 | 0% |
| 6 人、花色规范化、每人 4 单位 | 50 | 200 | 0 | 0 | 2.73% |

原始牌张六人的候选查询全部回退到与基线相同的均匀策略，因此成对增益及其标准误为零。其他三组在这些有限随机轨迹上的收益也相同：覆盖信息集可能仍保存初期均匀平均策略，有差异的概率也未必在这批抽样中改变行动。零标准误不表示已经精确证明策略等价或收益稳定。花色规范化提高了本次查询覆盖，但尚无可确认的收益优势。

覆盖率是这些评测查询的比例，不是整个德州信息集空间的覆盖率。以上都是单一种子的规模与正确性检查，后续需要覆盖扩大、多种子和独立对手评测。

## 画像与行动后验

`opponent.py` 是 Pluribus 基线之外的实验扩展；默认训练／搜索不依赖画像。输入是明确的公开观察和机会事件，模块不会从盲注、弃牌或未公开牌自动生成 VPIP、诈唬等标签。

```python
from texas_strategy.opponent import OpponentModel, ProfileEvent, ScopedPlayer

player = ScopedPlayer("offline-session-1", "table-1", "seat-2")
model = OpponentModel()
model.record(
    ProfileEvent(
        event_id="hand-1:pfr",
        player=player,
        context="preflop:button:unopened",
        observed_at=10,
        available_at=20,
        kind="opportunity",
        name="pfr",
        eligible=True,
        success=True,
    )
)
posterior = model.stat(
    player,
    "preflop:button:unopened",
    "pfr",
    as_of=20,
    prior_alpha=2,
    prior_beta=8,
)
assert (posterior.successes, posterior.opportunities) == (1, 1)
assert posterior.mean == 3 / 11
assert model.stat(player, "preflop:button:unopened", "pfr", as_of=15).opportunities == 0
```

`available_at` 表示观察何时可用于决策；整手完成后才能确定的统计不能泄漏给先前节点。时间是调用方统一的非负逻辑时钟。`eligible=False` 不增加分子或分母；成功必须有真实机会。事件按会话、牌桌、玩家作用域和 event ID 去重，冲突拒绝；`rebuild(events)` 用修正后的日志原子替换旧计数。不能按昵称自动跨账号合并。

`BetaPosterior` 保留原始成功／机会数、Beta 参数、时间窗、均值、方差及模型版本。`interval()` 是基于 Chebyshev 的保守后验质量界，**不是**精确等尾分位数区间。`OpponentModel.actions(...)` 对相同上下文及完全相同合法动作集合做 Dirichlet 收缩，返回原始次数、后验参数和行动概率；check/bet 机会不会混进 fold/call/raise 分母。

```python
from texas_strategy.opponent import bayesian_range_update, blend_experts

updated_range = bayesian_range_update(
    prior={("As", "Ah"): 0.2, ("Kd", "Kh"): 0.5, ("Qc", "Qd"): 0.3},
    likelihoods={("As", "Ah"): 1.0, ("Kd", "Kh"): 0.2, ("Qc", "Qd"): 0.8},
    known_cards=("As", "2h", "3d"),
)
# As 阻断 AA；其余按 prior(hand) * P(public_action | hand, context) 归一化。

candidate = blend_experts(
    baseline={"fold": 0.5, "call": 0.5},
    experts={"example": {"fold": 0.2, "call": 0.8}},
    distances={"example": 0.1},
    confidence=0.4,
    sample_count=100,
)
assert candidate.adaptation_weight == 0.25
assert not candidate.equilibrium_guarantee
```

范围 likelihood 必须来自决策当时可用的、按手牌和上下文条件化的行动模型；上例数值只是构造的模拟输入。不能从 VPIP 唯一推导范围，不能用未来摊牌充当当前 likelihood。阻断牌、重复组合、缺失 likelihood 和零概率证据都有检查；不可能证据不会静默变成均匀范围。

专家权重采用本项目的 `softmax(-distance / temperature)`；缺失路径 `None` 不分配权重。默认少于 20 个有效样本、零置信度或没有覆盖模板时返回基线；调整权重上限默认 0.25。置信度由调用方提供，并非已训练校准器的输出。这个上限不是最坏收益约束，混合不保证保留均衡或提升收益。当前没有把画像专家自动接入最终行动推荐；需先用独立效用比较和误判压力测试验证。

## 画像假设与博弈最优响应的可验证衔接

`adapt` 子命令用明确的模拟对手策略演示“行为模型 → 最佳响应候选 → 独立收益评测 → 风险门控”。它与上面的距离专家混合分开：这里在有限游戏中计算对声明对手策略的完整最佳响应，再逐项检验与 blueprint 的混合策略。

```bash
uv run --frozen python -m texas_strategy.cli adapt \
  --game kuhn --players 2 --blueprint artifacts/kuhn-2p.json \
  --opponent-mode call --synthetic-samples 200 --min-samples 50 \
  --mixing-weights 0.05,0.1,0.25 --worst-case-loss 0.05 \
  --require-coverage --report artifacts/kuhn-2p-adaptation.json
```

`--opponent-mode` 选择 `blueprint/call/raise/fold` 后续策略偏置，构造一个已知模拟对手。`--synthetic-samples` 仅是这次模拟声明的样本支持，默认 0；不是采集到真实玩家数据的数量，也不是概率置信度。真实画像仍需训练和校准条件行动模型后再接入。

候选首先要相对 baseline 获得正的模型内预期收益，随后在两人零和有限游戏中重新求对方最佳响应，检查相对 baseline 的最坏情况损失是否在 `--worst-case-loss` 预算内。输出列出每个候选的模型收益、最坏情况价值、门控结果和最终权重。数据不足、不支持多人最坏情况保证、预算不足或候选未通过时保留 baseline；这个门控不意味着 baseline 本身已是均衡，也不把模型内收益保证推广到未知真实对手。

已使用上述 3,000 次 Linear CFR 的两人 Kuhn blueprint 实际运行这组实验，固定风险预算为 0.05、候选混合权重为 0.05／0.10／0.25。结果也记录在汇总 JSON 的 `adaptation_experiments`：

| 明确的模拟对手 | 接受的混合权重 | 精确模型收益增量 | 相对基线最坏情况价值下降 |
| --- | ---: | ---: | ---: |
| call 偏置 | 0.10 | +0.013635 | 0.018650 |
| raise 偏置 | 0.25 | +0.026499 | 0.026950 |
| fold 偏置 | 0.10 | +0.010686 | 0.035174 |

例如 call 模型下，0.25 权重虽然带来更大的模型收益 `+0.034086`，但最坏情况下降约 `0.061286`，超过 0.05 预算而被拒绝。样本支持设为 0 时回退原因是 `insufficient_samples`；换成三人 Kuhn 时为 `multiplayer_guard_unsupported`。这验证了候选收益与误判风险可以分开检查，仍未验证真实玩家画像的准确性或真实收益。

## 蘑菇会话账本

`mushroom.py` 只完成规则明确前提下的离线会话账本，未训练蘑菇策略。截图规则来源和真实平台待确认项见 [mushroom-rules.md](mushroom-rules.md)。下列配置明确声明的是模拟选择，不能作为 WPK 已验证规则。

```python
from texas_game_core.mushroom import MushroomLedger, MushroomRules

rules = MushroomRules(
    base_points=20,
    version="example-simulation-v1",
    contribution_timing="before_hand",
    roster_policy="explicit_dealt_players",
    entry_policy="between_hands",
    insufficient_policy="reject",
    freeze_policy="no_transfers",
    remainder_policy="participant_order",
)
ledger = MushroomLedger(rules)
players = ("a", "b", "c", "d")
ledger.begin_hand(
    "h1:begin", "h1", players, "a", dict.fromkeys(players, 1000), timing="before_hand"
)
assert ledger.snapshot().pool == 100
reward = ledger.settle_hand("h1:end", "h1", "a", outcome="sole_main_pot")
assert reward.transfers == (("a", 100),)
assert reward.reveal_required == ("a",)
assert ledger.snapshot().check_conservation()
```

所有玩家缴纳一份，小盲额外一份；少于配置人数时冻结并按声明策略停止转账，人数恢复时保留历史池额。非小盲合格收池只结转，小盲合格独赢领取整池并产生亮牌要求；亮牌要求不提供或推断未知牌面。`outcome` 必须显式声明，平分／边池／未知资格返回 `UnsupportedRuleError`。

`end_session(event_id)` 是调用方明确触发的事件，最终池额均分给最后一手参与者，冻结状态也不丢失余额。`participant_order` 将整数余数按输入参与者顺序逐个分配，调用方必须稳定提供顺序；`reject` 在不能整除时拒绝。未声明缴纳时点、人数口径、入退桌、冻结行为、筹码不足或余数策略时，对应操作拒绝执行。筹码不足采用原子拒绝，不偷偷允许部分缴纳或借款。

`record`／`rebuild` 支持幂等重放；冲突 ID、重复手局或乱序结算拒绝。重复事件返回空转账和空亮牌要求，不再次扣款或派彩。从空池开始始终保持 `sum(player_net) + pool == 0`。这是一份独立池账本；外部筹码来源若已包含这些转账，不能再加一次。普通德州策略还没有包含跨手池价值、位置轮换和终局分配，因此不能把本账本加到普通策略旁边便称为蘑菇均衡。

## 下一阶段的算法工作

1. 扩大多人标准德州的可控信息集抽象和独立覆盖检查，先测量内存、吞吐和方差，再决定服务器训练规模。
2. 将标准德州公共信念构造和多后续策略搜索扩展到明确预算的范围采样，并验证全部玩家的私牌互斥、到达权重和全范围行为一致性。
3. 在独立对手及时间切分样本上检验画像校准、范围更新和调整收益，保持关闭画像时基线可独立运行。
4. 在完整声明规则的蘑菇模拟会话中构造跨手效用、训练相应 blueprint；保持真实平台规则缺口显式。

这些工作属于后续算法扩展；现有单次实验和 CLI 成功运行不能替代这些验收。
