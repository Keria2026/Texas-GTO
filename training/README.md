# 自博弈训练入口

此目录预留给经过验证的 Pluribus 方法自博弈作业；当前尚无训练命令或模型权重。

实现顺序：小博弈精确参照 → MCCFR／Linear CFR → 可恢复 blueprint → 多后续策略搜索 → 标准六人有限预算实验。接口来自 `packages/game-core`，策略基础函数来自 `engines/strategy`；详细里程碑见 [决策引擎](../docs/decision-engine.md)。

作业配置需保存规则、动作／信息抽象版本、种子、迭代预算、内存限制、剪枝开关与检查点路径。停止与恢复不得改变累积权重或静默重置平均策略。工件写入被 Git 忽略的 `artifacts/` 或 `checkpoints/`；独立评测通过后才注册为可用 blueprint。

首轮训练使用 CPU 小任务测量正确性与资源。当前没有自动启动大规模训练、申请付费服务器或配置 GPU 作业。
