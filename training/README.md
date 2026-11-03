# 自博弈训练入口

当前主线入口为 `--algorithm pluribus`，按作者补充 Algorithm 1 实现，使用版本化的完成迭代调度；原 CFR/MCCFR 是对照。细节与限制见 [Pluribus 实现](../docs/pluribus-implementation.md)。

训练按每个席位的**期望净积分**优化，不按赢局次数、赢得的底池总额或是否入池计奖励。蘑菇模型收益为会话结算后积分减起始积分，缴费、下注、派奖和退款只计一次。价值下注应比较“尺寸 × 被较弱范围支付的概率”及被更强范围反制的后续损失；跟注上限随条件范围与未来行动变化，历史投入不构成继续投入的理由。完整收益口径和验收见 [决策引擎](../docs/decision-engine.md#1-优化什么)。

自博弈学习面对训练对手策略时的动作价值，不会自动识别真实玩家的支付习惯。真实牌局的范围／回应模型、针对性调整及其独立收益评测属于基础 Pluribus 之上的适应层，目前尚未接通实局推荐。

## 服务空闲时自动续训

`python -m texas_analysis` 默认启动一个独立、低优先级 CPU 训练进程，无需先打开页面或提交训练任务。当前固定使用八人各 800 分、2/4/8、两手视野、`abstraction=mushroom_context` 的蘑菇模型；每次完整迭代在同一个 `TEXAS_DATA_DIR/core/training.sqlite3` 事务中保存策略表、轮数和 RNG，服务重启校验后续训。首次启动会校验并迁移原 `checkpoint.json`，保留原文件；已有数据库时以数据库为准，损坏不会回退旧 JSON。工作台优先显示“核心策略自动训练”；Kuhn／Leduc 手工训练收进开发工具。

上下文模型使用可见牌聚类、相对位置、筹码档与当前手行动序列，共享相似局面的策略并翻译回真实合法金额。完整记忆的旧模型不能直接混入新信息集；升级已有服务前须停服归档旧 core 工件。不会在遇到不兼容工件时静默重置。抽象、模型版本和验证边界见[上下文说明](../docs/mushroom-context.md)。

HTTP 写入／计算请求、WPK `capture.append` 和排队／运行的手工作业会让自动训练暂停；空闲 30 秒后继续。健康检查、训练状态轮询和 WebSocket 心跳不会阻止训练。暂停保留正在遍历的栈和随机状态，避免每次有请求就重做长迭代；服务关闭则回到最后完整保存的迭代。检查点序列化已经开始时允许完成原子写入，单个规则函数也不是强制抢占。

`GET /v1/core-training` 返回真实阶段、已保存迭代、遗憾信息集、已积累平均策略的信息集和检查点大小（主库、WAL、SHM 实际文件字节之和，日志回收后可下降）；本地仍要求 `X-Texas-Local: 1`。`quality=finite_horizon_unvalidated`、`recommendation_ready=false` 表示尚不能用于实局。平均策略遵守原有积累周期，早期有遗憾而无平均策略是正常且明确可见的状态。

工作进程地址空间上限 1 GiB。全局策略表存于 SQLite，页缓存 8 MiB；全表折扣与快照流式处理，不复制全部信息集。数据库可超过 64 MiB；64 MiB 只保留为旧 JSON 导入限制。本轮遍历的工作字典仍驻留内存，单轮过大仍会报告 `limited` 并保留最后已提交进度；这不是无限规模训练的保证。损坏／不兼容的导入报告错误，不静默新建；同一目录只允许一个训练写入者。`TEXAS_AUTO_TRAIN=0` 可关闭后台训练，不影响连接及分析 API。

用户已指定算法服务内存最多 1G。本机 systemd 另外设置 `MemoryMax=1G`，将 API 与训练子进程合计限制为 1 GiB；后续扩容不得提高该上限。该总内存预算不等于检查点文件上限，也不覆盖 Chrome／Vite 等其它进程。

训练子进程意外退出时，服务按 1／2／4 秒退避从保存进度恢复，每个服务实例最多三次，并报告恢复次数。容量限制、明确错误、正常停止和已有其它写入者不会自动重试。每次新进程使用独立的无锁布尔控制信号，父级关闭后不再复活子进程。旧 JSON 模型曾在 267 轮达到文件上限；数据库迁移保留这些轮数和数值，不重新训练冒充续训。

核心训练数据库使用独立备份命令导出单文件快照，允许训练同时运行，不生成全表 blueprint：

```sh
uv run --frozen texas-research checkpoint-backup \
  --source artifacts/local/core/training.sqlite3 \
  --output artifacts/core-export/training.sqlite3
```

输出路径必须尚不存在。备份使用 SQLite backup API，包含已提交 WAL 内容；不要直接复制运行中的主文件。迁移到另一台机器时，把快照置于新数据目录的 `core/training.sqlite3`，使用同版本后端启动，规则、初始局面与逐条数值校验通过后自动续训。替换现有训练库前先停止服务并备份整个旧 core 目录（含可能存在的 WAL/SHM）；不要把旧 WAL 留给替换后的主文件。旧 JSON 的 `train/resume` 与 blueprint 导出继续用于小规模离线研究，不接受 SQLite 输入。导出成功不表示策略经过实局质量评测。

## 算法开发入口

训练、续训与评测已由 `texas-research` CLI 提供，实现位于 `engines/strategy/src/texas_strategy/cli.py`。完整命令和真实数值见 [离线算法说明](../docs/offline-algorithms.md)及[实验汇总](../docs/experiments/offline-baseline.json)。

在线搜索所需的抽样后续评估可用 `uv run --frozen python training/verify_sampled_search.py` 独立复现。它检查 Pluribus blueprint → MCCFR 多后续策略搜索的小博弈闭环，以及八人 800 分蘑菇的合成信念搜索；不读取 WPK 数据，不把合成范围／均匀后续策略标为训练成果，也不替代自动训练。范围和耗时见 [抽样搜索实验](../docs/experiments/sampled-search.json)。

```sh
uv run --frozen texas-research train --game kuhn --players 2 \
  --algorithm pluribus --weighting linear --iterations 3000 \
  --blueprint artifacts/kuhn.json --checkpoint checkpoints/kuhn.json
```

保存的 JSON 工件绑定规则、算法、权重、迭代数、种子、信息集行动支持和校验和；checkpoint 另外保存 RNG 与累计遗憾／平均策略。新 checkpoint 使用紧凑 schema 2，仍可导入 schema 1，blueprint 保持 schema 1。上下文模型的 checkpoint 还固定训练起始筹码与按钮，以免策略可跨局查询时意外改变续训分布。续训不得改变规则或静默重置平均策略。工件与牌谱被 Git 忽略。

八人蘑菇跨手模型已接入同一个训练器、CLI 和 HTTP `/v1/training`。以下命令用于开发验证，不是最终产品的手动训练流程：

```sh
uv run --frozen texas-research train --game mushroom --players 8 \
  --abstraction mushroom_context \
  --small-blind 2 --big-blind 4 --straddle 8 --session-hands 2 \
  --algorithm pluribus --iterations 1 --seed 42 --seconds 30 \
  --blueprint artifacts/mushroom.json --checkpoint checkpoints/mushroom.json
```

默认每席 800 分，跨手保留实际筹码、池额、缴费归属和信息边界；`--session-hands` 是明确的有限训练视野，末尾按实际缴费退还余池，与无限期真实会话不同。视野、规则及信息编码进入策略工件兼容性检查，起始筹码另外绑定 checkpoint 续训上下文；不能拿不同视野的检查点静默续训。省略 `--abstraction` 可运行完整记忆模型作离线对照，它不兼容当前核心模型的检查点。

新建蘑菇模型默认 `average_mode=sampled_uniform`、`average_samples=16`、`strategy_interval=1`：每轮每席采样 16 条翻前路径，以对手前缀采样概率纠正累计量。原枚举算法与旧工件继续使用原配置；不会因为升级代码就悄悄改写旧平均。显式提供旧式配置但未写 average_mode 时仍为枚举模式。样本少、深层权重大时仍可能方差很高；有平均条目不表示策略成熟。未覆盖节点仍拒绝推荐，详见[平均策略实现](../docs/pluribus-implementation.md#采样平均策略扩展)。

尚缺生产信息／动作抽象、长期会话的延续价值、re-straddle 和真实输入匹配。自动训练候选不会自动成为实局推荐来源。

完整遍历用于可验证小博弈；德州只接受有预算的 MCCFR 实验和抽样对照。尚无训练成熟的六人策略库，稀疏工件的默认均匀回退不算覆盖。根本优化方向是抽象和测量后的训练优化，不能靠增加一条服务命令声称已有超人水平。

目标生产架构将全部训练作为服务器作业运行，与扩展实时分析的资源隔离；现有 CLI 为该作业的算法入口。大规模作业部署在独立服务器 CPU 进程，显式设置 `--seconds`、`--max-nodes` 和操作系统内存约束；当前未配置付费基础设施或 GPU 任务。

核心策略可通过 `texas_strategy.persistent_policy.policy_snapshot(game, database)` 的短只读事务按信息集查询。它固定同一已提交轮次，检查规则与合法动作，只归一化已有平均策略；默认缺失即报错。该组件尚未替代 WPK 公开状态适配、公共范围搜索和推荐协议。已有库升级采样平均须停训练并备份，再调用 `store.enable_sampled_average(trainer)`；仅在第一下注轮平均全部为零时允许，保留所有遗憾、后续平均与 RNG，并把开始轮次写入配置。非零旧翻前平均不会被丢弃或静默混用。
