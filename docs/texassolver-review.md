# TexasSolver 源码学习与接入评估

日期：2026-10-31。项目定位按用户最新要求调整：Pluribus 为核心算法；TexasSolver 作为标准两人翻后的可选对照工具，可在明确覆盖和质量后研究局部标签生成。已在当前机器编译 console 分支并完成有限验证，尚未接入 Texas-GTO 应用。其研究结果保留，但不将接入它作为主线开发前提。

## 1. 版本与核查范围

- [主仓库](https://github.com/bupticybee/TexasSolver) master：`bb53f8cbfff4841da2d75bd060d0ab269c3d5881`，读取 README、许可、核心求解与 GUI／命令行接口代码，未编译 GUI。
- console：`6dfb65b4d7ed081da509e8d8c3d82138c4708267`，实际构建和执行对象。以下源码链接固定至此提交，除非另有说明。
- 两个分支功能不同：master 的范围字符串解析器未见四字符具体组合分支；本次 console 快照支持 `AsAh:0.5` 等输入，并针对具体组合禁用花色同构。不能用一个分支的测试证明另一个分支。
- README 对 PioSolver、Java 版本和另一个 GPU 产品的速度比较均为作者声明，本次未复现，不能用作本项目性能承诺。

## 2. 求解原理

输入双方加权范围、公共牌、底池、有效筹码和各街允许的下注尺寸，构造包含行动、发牌及终局节点的有限博弈树。遍历双方私牌与公共发牌，计算反事实收益，更新遗憾并生成策略分布，定期对平均策略计算最佳响应指标。迭代达到阈值或次数上限后导出策略树。

| 模块 | 源码 | 学习要点 |
| --- | --- | --- |
| 树和收益 | [GameTree.cpp](https://github.com/bupticybee/TexasSolver/blob/6dfb65b4d7ed081da509e8d8c3d82138c4708267/src/GameTree.cpp) | 分离行动节点、发牌节点、摊牌与弃牌收益；下注尺寸决定搜索空间 |
| 并行求解 | [PCfrSolver.cpp](https://github.com/bupticybee/TexasSolver/blob/6dfb65b4d7ed081da509e8d8c3d82138c4708267/src/solver/PCfrSolver.cpp) | 两名玩家交替遍历；OpenMP 并行；可选花色同构；命令行使用 discounted_cfr |
| 遗憾和平均策略 | [DiscountedCfrTrainable.cpp](https://github.com/bupticybee/TexasSolver/blob/6dfb65b4d7ed081da509e8d8c3d82138c4708267/src/trainable/DiscountedCfrTrainable.cpp) | 正遗憾归一化决定当前策略，累计策略用于输出；无正遗憾时均匀分配 |
| 最佳响应 | [BestResponse.cpp](https://github.com/bupticybee/TexasSolver/blob/6dfb65b4d7ed081da509e8d8c3d82138c4708267/src/solver/BestResponse.cpp) | 固定对方平均策略后求最佳响应，形成当前抽象树的误差诊断 |
| 范围解析 | [PrivateRangeConverter.cpp](https://github.com/bupticybee/TexasSolver/blob/6dfb65b4d7ed081da509e8d8c3d82138c4708267/src/tools/PrivateRangeConverter.cpp) | 范围类展开、具体组合、权重、重复与公共牌冲突处理 |
| 入口与导出 | [CommandLineTool.cpp](https://github.com/bupticybee/TexasSolver/blob/6dfb65b4d7ed081da509e8d8c3d82138c4708267/src/tools/CommandLineTool.cpp)、[PokerSolver.cpp](https://github.com/bupticybee/TexasSolver/blob/6dfb65b4d7ed081da509e8d8c3d82138c4708267/src/runtime/PokerSolver.cpp) | 文本配置、线程数／预算／目标精度、JSON 树导出 |

这里的 `train()` 指本次场景的 CFR 迭代，不是必须先训练一个大型神经网络。可以在 CPU 服务器按场景求解，也可离线批量生成基线，再考虑策略蒸馏加速。

源码的折扣更新属于具体实现，不能只依据类名断言其等同论文中的所有 DCFR 变体。例如累计策略中存在 `theta=0.9` 的折扣，乘以 reach_probs 的部分被注释。进一步复现理论算法时需逐项核对更新顺序和权重；本次冒烟不构成其收敛证明。

## 3. 接入画像时必须处理的细节

1. **范围与行为模型分开。** 画像可改变进入当前节点的手牌权重；求解器仍让双方在给定树上优化策略。只修改范围不能表达“此人拿这些牌仍会过度跟注”。已查 CLI／训练入口未提供对手节点策略锁定接口；行为剥削需另建固定对手策略评估、受约束最佳响应或专家策略层。
2. **保留组合级信息。** console 支持具体组合，避免把 `AhKh` 与其他 AKs 混为一谈。其当前入口遇到具体组合会禁用花色同构；不要为提速强行打开。已有一个测试专门展示底层换色函数在非对称范围上的错误行为，测试通过不代表该底层操作已修复。
3. **权重不是归一化后可直接传入的概率。** 解析器丢弃 `weight <= 0.005` 的项。宽范围的单组合后验概率往往都低于该阈值，直接传入可能清空范围。适配器需采用保留相对比例的统一缩放（如除以最大权重），量化剩余截断质量；不能悄悄丢弃大量概率质量。全零、非有限、重复组合等输入在应用层拒绝。
4. **街道与行动根节点。** CLI 只接受 3／4／5 张公共牌，不能据内部存在 preflop 枚举就声称 CLI 支持翻前。`set_pot` 把双方 commit 均设为底池一半；`set_effective_stack` 在此基础上设置总 stack，所以必须先设底池。根节点构造用于约定的街道起点，不能直接代表任意面对下注的中途状态。
5. **历史与范围对齐。** 若从街道起点建树，应输入起点范围，再沿实际行动路径查询对应节点；不要把已经按该路径更新的后验放回起点再重复更新。超出树的尺寸应重新求解或声明不支持，不能任意挑最近分支。
6. **金额转换。** BET／RAISE 的输出金额来自内部投入增量，不能直接当作本项目的 `raise_to`。代码对底池比例、跟注差额、最小单位取整与 all-in threshold 有自己的处理；适配器必须逐动作重放核对整数筹码与合法性。
7. **误差口径。** `set_accuracy 0.5` 是求解器报告的百分比阈值：双方最佳响应值之和 ÷ 2 ÷ 初始底池 ×100，不是 99.5% 胜率或预测准确率。单独的玩家值可为负；需看组合诊断及其博弈假设。
8. **导出能力。** 本次 console JSON 给出 actions、player、strategy、childrens 等，包含每个具体组合的动作频率；没有直接导出可用的逐动作 EV。不要从两个最佳响应日志数值伪造 hero 的动作 EV。求解日志与 JSON 分别校验，不能仅凭退出码判成功。

当前求解器固定两名玩家。已查 CLI 与规则实现未发现抽水、边池、蘑菇池、跨手位置轮转的完整配置。此类场景先报告不支持。即使多人局后来剩两人，也需验证历史、范围、死钱、资格与资金口径能否准确表示。

## 4. 本次实际验证

环境：Linux x86_64，GCC 14.2.0，CMake 3.31.10，OpenMP 4.5。CMake 临时安装在研究目录，项目未新增依赖。使用 Release、2 个编译任务，求解使用 2 线程。第三方源码未修改。

| 检查 | 结果与范围 |
| --- | --- |
| 构建 | console_solver 与 test 成功；旧第三方 CMake 配置有警告，无构建失败 |
| 原仓库测试 | 7 项执行并通过：具体组合解析、非法／死牌组合、同构标志、求解入口同构降级及换色缺陷示例 |
| 排除的测试 | test_poker_solver_bench 未运行，依赖正常构建不生成的 `../install/tree.km`；不是通过或预期失败 |
| 范围类河牌样例 | 退出码 0；日志最后 Iter 41，自报误差 0.49999952%；6 个行动节点、105 行组合策略；频率最大归一误差约 7.45×10^-8 |
| 具体组合加权河牌样例 | 退出码 0；日志最后 Iter 21，自报误差 0.4048586%；6 个行动节点、18 行组合策略；请求同构但入口按预期禁用 |
| 结构校验 | 两个样例均验证频率有限、在 [0,1]、和为 1、动作维度匹配；范围类样例排除公共牌冲突；具体组合样例逐项核对所属玩家的输入组合 |
| 单次冷启动测量 | 范围类 2.619 秒／峰值 RSS 107164 KiB；具体组合 2.920 秒／107160 KiB，含字典加载和文件输出，不代表服务 p95 或大树性能 |

这些结果证明有限输入下能运行、导出及满足所检验的结构约束；未独立证明全树收益正确、完整牌型正确或实际扑克策略强度。未运行 GUI、完整翻牌基准、PioSolver 对照、短牌或多人／蘑菇实验。

复现输入保存在 [范围类样例](experiments/texassolver/river-classes.txt) 和 [具体组合样例](experiments/texassolver/river-exact.txt)，两者由本次研究编写。结果摘要见 [validation.json](experiments/texassolver/validation.json)。在固定 console 提交下：

```sh
# 在 TexasSolver console checkout 根目录；使用 CMake 3.31.10
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target console_solver test --parallel 2
# 在其 build 目录执行，以满足原测试的相对资源路径
./test --gtest_filter=-TestCase.test_poker_solver_bench
# 在每个样例的独立输出目录执行，传绝对路径，避免覆盖结果
/path/to/TexasSolver/build/console_solver -i /path/to/river-classes.txt -r /path/to/TexasSolver/resources -m holdem
```

## 5. 对本项目的采用方式

项目主线为 [Pluribus 自博弈与搜索](pluribus-paper.md)。如需两人局部对照，可建立隔离的 `TexasSolverProvider`：严格检查覆盖与输入，生成配置，在独立作业目录执行，解析 JSON 与收敛日志，转换成统一协议。服务器控制 CPU 并发、内存和截止时间；该工具不决定默认引擎或开发顺序。

比较前对齐规则、范围、下注树和金额语义，记录双方求解预算与误差。频率不同不直接代表实现错误；还需比较策略价值并使用独立可穷举算例。第三方输出不作为正确性真值；超时或未达质量门槛不输出“已验证最优”。

[对手建模论文](opponent-modeling-paper.md) 支持研究主线之上的画像增强；修改 TexasSolver 的 range 参数不能代替行为专家训练。多人和蘑菇扩展沿 Pluribus 主线推进，需扩展状态、各玩家收益及继续价值；本工具的两人范围与测试结果不能证明这些能力。

## 6. 许可证事实

仓库 [LICENSE](https://github.com/bupticybee/TexasSolver/blob/6dfb65b4d7ed081da509e8d8c3d82138c4708267/LICENSE) 为 AGPL-3.0；master README 另有商业授权说明，部分表述与许可证正文需要进一步核对。修改版通过网络供用户交互时，AGPL 第 13 节涉及提供对应源码的义务；子进程封装不自动消除许可问题。正式分发或服务接入前明确适用的开源或授权方案。本次仅在临时目录研究与验证，未将上游代码复制进 Texas-GTO。
