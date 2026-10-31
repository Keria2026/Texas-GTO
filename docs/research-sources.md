# 决策算法参考资料核查

核查日期：2026-10-31。核查范围包括最初七个链接、后续两个 PDF 链接、TexasSolver 仓库，以及用户上传的对手建模论文和 Pluribus 的 Science 论文。本文区分已读源码／正文、已执行验证、未取得正文和待复现实验；不以标题、README 或经验知识代替原文结论。

项目采用优先级：用户指定 Pluribus 为核心算法，优先构建自博弈 blueprint 与实时搜索；对手建模论文服务于后续画像增强，TexasSolver 用于局部对照，其他路线作为研究补充。此前的源码与实验记录保持其原有范围，不因优先级调整而视作 Pluribus 的实现证据。

## 访问记录

| 资料 | 本次取得内容 | 状态 |
| --- | --- | --- |
| [知乎 480674835](https://zhuanlan.zhihu.com/p/480674835) | 无正文 | 环境代理 CONNECT 返回 403 |
| [自动化学报 DOI 10.16383/j.aas.c210127](https://www.aas.net.cn/cn/article/doi/10.16383/j.aas.c210127) | 已通过用户上传取得完整 14 页 PDF | 正文已读、关键公式已对照页面；网页下载仍受阻，论文实验未复现 |
| [中科院自动化所 2021-12-10 页面](https://ia.cas.cn/kxyj/kydt_1/202112/t20211210_6293703.html) | 无正文 | 环境代理 CONNECT 返回 403 |
| [deeptexas-ai/deep-holdem-ai](https://github.com/deeptexas-ai/deep-holdem-ai) | README、许可证、求解器、模型、配置及部分测试源码 | 静态核查完成，未构建或运行训练／测试 |
| [博客园 15847129](https://www.cnblogs.com/xingzheai/p/15847129.html) | 无正文 | 环境代理 CONNECT 返回 403 |
| [知乎 1989734865961243528](https://zhuanlan.zhihu.com/p/1989734865961243528) | 无正文 | 环境代理 CONNECT 返回 403 |
| [CSDN 96406166](https://blog.csdn.net/u9Oo9xkM169LeLDR84/article/details/96406166) | 无正文 | 环境代理 CONNECT 返回 403 |
| [TexasSolver](https://github.com/bupticybee/TexasSolver) | master／console 源码、许可、构建、测试与河牌输出 | 7 项相关测试通过、2 个河牌样例验证完成，见专项记录 |
| [Science DOI 10.1126/science.aay2400](https://www.science.org/doi/epdf/10.1126/science.aay2400) | 已通过用户上传取得正文 PDF，共 7 页（含 1 页出版信息） | 正文已读并核对图 4；补充材料未取得，算法与实验尚未复现；此前网页下载受代理阻断 |

初次已在环境配置草稿中追加五个精确域名：zhuanlan.zhihu.com、www.aas.net.cn、ia.cas.cn、www.cnblogs.com、blog.csdn.net，保留原有预设。随后追加专利域名，本轮又追加 `www.science.org`，草稿均保存成功。保存不等于运行时生效；需在环境设置中审阅、保存并发布，然后重新检查访问结果。不能将这些 403 描述为文章不存在或网站拒绝爬取。已上传论文不再受正文获取阻塞，其余未读资料保持未读状态。

## PDF 的下载与上传状态

| 用户提供的资料 | 本次下载结果 | 尚未核实的内容 |
| --- | --- | --- |
| [自动化学报 PDF 导出](https://www.aas.net.cn/article/exportPdf?id=3acb9adb-0393-463b-9141-bda83c7d090b) | 链接下载为 0 字节；随后用户上传同主题论文 | 上传文件已核实 DOI c210127；与此导出端点的文件字节一致性未核验 |
| [CN112691383A PDF](https://patentimages.storage.googleapis.com/5c/94/68/f15d6eeaacae13/CN112691383A.pdf) | 代理连接阶段返回 403，下载字节数为 0 | 专利名称、申请人、算法步骤、权利要求与实验内容 |
| 用户上传 `19-Science-Superhuman.pdf` | 本地文件可读；DOI、正文与图表已核实，校验和见专项笔记 | 未包含 Supplementary Text、Table S1、Data File S1；精确复现参数与完整实验协议仍待核查 |

此前下载时确认运行环境的网络策略已执行，但自定义域名列表仍为空；已有草稿尚未应用。追加的域名均已保存到草稿，工具返回需要发布。此处的拦截来自环境网络访问阶段，不是源站返回 PDF 正文后的解析错误。本轮直接读取本地 Science 附件，无需等待网络配置发布。

对尚未取得的专利，后续执行以下核查；已上传的 c210127 和 Science 正文已完成文献理解与项目映射：

1. 核对文献身份，记录文件校验和与引用页码；区分论文算法、专利权利要求和本项目的工程推论。
2. 提取输入状态、对手模型、优化目标、策略更新、训练／推理流程及停止条件；核查是否真的利用玩家行为历史。
3. 核对理论假设、实验对手、收益单位、算力与误差，区分文献报告结果和本项目复现结果。
4. 对照决策引擎中的基线、后验、最佳响应和调整约束，写明可复用步骤、缺失前提，以及多人蘑菇跨手规则需要增加的状态和收益项。
5. 更新决策方案和 AGENTS.md 中有原文依据的约束；不以专利公开文本本身作为算法有效、全局最优或已获授权的证据。

两篇已上传论文的阅读笔记见下节。专利 CN112691383A 仍未取得，不将其内容并入已核实的文献结论。已读论文的作者实验报告也不等于本项目复现结果。

## 新完成的论文阅读与 TexasSolver 验证

- [对手建模论文笔记](opponent-modeling-paper.md)：确认题名、作者、DOI 与文件校验和；记录演化专家池、条件特征树、风格度量、策略集成与回退；对照第 1010 页原图识别式 (3) 的方向和归一化疑点，区分原算法与项目修正。原文多处限制为两人、每手重置筹码，不能直接推广到蘑菇跨手局。
- [Pluribus 论文笔记](pluribus-paper.md)：确认 Brown、Sandholm，Science 2019，365(6456):885–890，DOI aay2400；记录改进 MCCFR、Linear CFR、抽象、全范围搜索、隐蔽选择四种后续策略与 rollout。核对原文 CPU／内存、1–33 秒搜索、AIVAT、标准误与单侧检验。原算法不根据对手牌风适应，也未保证多人纳什收敛。
- [TexasSolver 源码核查](texassolver-review.md)：master `bb53f8cbfff4841da2d75bd060d0ab269c3d5881`；实际编译 console `6dfb65b4d7ed081da509e8d8c3d82138c4708267`。7 项原有相关测试通过，另一个缺少预制树的基准未执行。两个小河牌样例通过导出和结构校验，未证明完整策略质量。
- 设计以 Pluribus 为核心：默认策略引擎实现自博弈与搜索闭环；画像另由行为专家和误判控制增强，TexasSolver 提供局部对照。对手建模论文的基础网络不等于 GTO，TexasSolver 的加权手牌范围不等于完整对手行为模型，Pluribus 的后续策略选择也不等于风格专家加权。
- 蘑菇跨手收益是本项目新增问题；两篇论文均未直接验证该规则。Pluribus 补充材料、精确实现参数和完整实验协议继续列为待核查项，不阻塞已有正文结论的设计落地。

## GitHub 源码核查

快照：默认分支 main，完整提交 `f5fb2d1d4f043564facf1a0a63d28bcbfc942eec`。仅在临时目录获取研究副本，未复制到本项目或加入项目依赖。以下链接固定到该提交。

| 发现 | 源码证据 | 对项目的影响 |
| --- | --- | --- |
| 存在实质求解代码 | [game/subgame_solving.py](https://github.com/deeptexas-ai/deep-holdem-ai/blob/f5fb2d1d4f043564facf1a0a63d28bcbfc942eec/game/subgame_solving.py) 包含 CFR、遗憾更新、平均策略、最佳响应与可剥削度计算入口 | 可参考接口与算法组织，存在代码不等于正确性已经复现 |
| 限定两人 | 同文件开头断言 `Game.NUM_PLAYERS == 2`；[texas_holdem_hunl.py](https://github.com/deeptexas-ai/deep-holdem-ai/blob/f5fb2d1d4f043564facf1a0a63d28bcbfc942eec/game/texas_holdem/texas_holdem_hunl.py) 定义 NUM_PLAYERS=2 | 无法直接用于满四人启用的蘑菇游戏；多人底池后来剩两人也不等于从头两人牌局 |
| 价值模型使用范围信息 | [cfv/models.py](https://github.com/deeptexas-ai/deep-holdem-ai/blob/f5fb2d1d4f043564facf1a0a63d28bcbfc942eec/cfv/models.py) 输入包含公共状态与双方组合信念，输出每手牌的价值或策略 | 支持研究“公共状态＋范围→价值→搜索”的思路 |
| 在线入口未接入玩家画像 | [robot/rebel/solver.py](https://github.com/deeptexas-ai/deep-holdem-ai/blob/f5fb2d1d4f043564facf1a0a63d28bcbfc942eec/robot/rebel/solver.py) 的 `_compute_strategy_with_solver` 调用均匀初始信念 | 该入口不能直接承接本项目的历史范围后验；不据此否定其他递归流程的范围传播 |
| 默认推理配置不足以表明求解质量 | [conf/deploy/rebel.yaml](https://github.com/deeptexas-ai/deep-holdem-ai/blob/f5fb2d1d4f043564facf1a0a63d28bcbfc942eec/conf/deploy/rebel.yaml) 设置 num_iters=1、max_depth=4，引用 `./weights/value_net.net` | 一次迭代不构成收敛证据；完整跟踪文件清单未发现该权重，部署仍缺模型工件 |
| 部分评估入口未实现 | subgame_solving.py 的 compute_strategy_stats、compute_ev、compute_ev2 抛出 NotImplementedError | 必须建立独立评估闭环，不能按函数名声称支持 EV 评估 |
| CFR 测试是占位 | [tests/game/subgame_solving_test.py](https://github.com/deeptexas-ai/deep-holdem-ai/blob/f5fb2d1d4f043564facf1a0a63d28bcbfc942eec/tests/game/subgame_solving_test.py) 中 test_cfr 仅 pass | 即使该测试运行通过，也不能证明算法正确；本次没有运行测试 |
| 训练规模默认很大 | [conf/c02_selfplay/hunl_sp.yaml](https://github.com/deeptexas-ai/deep-holdem-ai/blob/f5fb2d1d4f043564facf1a0a63d28bcbfc942eec/conf/c02_selfplay/hunl_sp.yaml)：60 个 CPU 生成进程、回放容量 120,000,000、6 层 1536 宽网络、求解 1024 次迭代 | 不直接运行默认配置，先测小任务的内存与吞吐再配置服务器 |
| 运行依赖需要整理 | 根目录没有 requirements.txt 或 pyproject.toml；[cfv/selfplay.py](https://github.com/deeptexas-ai/deep-holdem-ai/blob/f5fb2d1d4f043564facf1a0a63d28bcbfc942eec/cfv/selfplay.py) 使用历史 PyTorch Lightning 接口；C++ 有旧 CMake／pybind11 构建配置 | 兼容性未验证，不能承诺开箱即用 |
| 许可来源混合 | [License.md](https://github.com/deeptexas-ai/deep-holdem-ai/blob/f5fb2d1d4f043564facf1a0a63d28bcbfc942eec/License.md) 含 MIT 文本和额外商业说明；cfv/models.py、run.py 等保留 Facebook Apache-2.0 文件头，另有第三方目录 | 复用须逐文件核实来源并保留许可证与声明，不能仅依据根目录文案给全部源码定性 |

结论：保留为 HUNL 搜索与价值网络的研究资料，不设为默认策略引擎或生产必需依赖，也不使用未经核验的权重。待 Pluribus 主线具有可重复基准后，再按收益与性能证据评估其中可用组件。

## 文章可访问后的核查任务

逐篇记录标题、作者、发表时间、所述算法及原始论文链接；对照算法解决的是两人／多人、零和／一般和、完全／不完全信息、静态／适应性对手中的哪一种问题。记录是否存在理论保证、保证依赖的假设、算力和实验协议，而非只抄最终胜率。

重点核查：CFR／MCCFR／Deep CFR 的关系；深度受限子博弈搜索与价值网络的作用；ReBeL 类公共信念状态建模；对手建模与受约束最佳响应；多人算法和跨手奖励的保证边界。此列表是后续阅读问题，不表示其余未读文章一定包含这些内容。

[决策引擎方案](decision-engine.md) 已明确 Pluribus 的核心地位、自博弈与搜索优先的交付顺序，以及 c210127 和 TexasSolver 的增强／辅助角色。整体决策模型尚未完成训练或独立对局验证；第三方河牌冒烟不等于本项目已经可运行。
