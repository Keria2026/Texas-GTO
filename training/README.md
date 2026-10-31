# 自博弈训练入口

训练、续训与评测已由 `texas-research` CLI 提供，实现位于 `engines/strategy/src/texas_strategy/cli.py`。完整命令和真实数值见 [离线算法说明](../docs/offline-algorithms.md)及[实验汇总](../docs/experiments/offline-baseline.json)。

```sh
uv run --frozen texas-research train --game kuhn --players 2 \
  --algorithm cfr --weighting linear --iterations 3000 \
  --blueprint artifacts/kuhn.json --checkpoint checkpoints/kuhn.json
```

保存的 JSON 工件绑定规则、算法、权重、迭代数、种子、信息集行动支持和校验和；checkpoint 另外保存 RNG 与累计遗憾／平均策略。续训不得改变规则或静默重置平均策略。工件与牌谱被 Git 忽略。

完整遍历用于可验证小博弈；德州只接受有预算的 MCCFR 实验和抽样对照。尚无训练成熟的六人策略库，稀疏工件的默认均匀回退不算覆盖。根本优化方向是抽象和测量后的训练优化，不能靠增加一条服务命令声称已有超人水平。

大规模作业可独立部署在服务器 CPU 进程，显式设置 `--seconds`、`--max-nodes` 和操作系统内存约束；当前未配置付费基础设施或 GPU 任务。
