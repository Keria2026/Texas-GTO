# 软件实现结构

目标部署为 **用户 Chrome 扩展实时获取 WPK 数据 → HTTPS/WSS 服务器 → 算法／训练 worker → 侧栏结果**，生产终端无需本地算法服务。完整职责和迁移方案见 [扩展与服务器架构](extension-server-architecture.md)。正式前端采用用户指定的 TGTO-FronEnd，具体代码与接入缺口见 [核查记录](tgto-frontend-review.md)。

下文记录当前可运行的离线研究工作台实现，不代表生产架构已经落地。算法依据是 [Pluribus 正文](pluribus-paper.md)，实现参数与实际实验见 [离线算法说明](offline-algorithms.md)。真实网站输入未接入；所有 HTTP 牌局是显式创建的模拟手牌。

## 当前研究版本的数据与算法流程

```mermaid
flowchart TD
  U[Chrome 侧栏 / 浏览器预览] --> A[本地来源校验 API]
  A --> E[SQLite 追加事件与版本校验]
  E --> G[不可变规则状态与观察]
  T[CFR / MCCFR 自博弈] --> B[JSON blueprint 与 checkpoint]
  B --> S[规则及信息集覆盖校验]
  G --> S
  S --> R[策略诱导公共根 / hero 全范围]
  R --> C[有限深度搜索 / 隐蔽后续策略选择]
  C --> U
  B --> V[独立信息集最佳响应评测]
  E --> O[公开行为画像 / 机会计数]
  O --> X[实验适应层 / 独立收益及风险校验]
```

规则／训练／评测使用同一 Python 博弈接口，但最佳响应单独实现，不复用 CFR 遗憾表来估计自身误差。TypeScript 负责交互和传输校验。没有第二套前端扑克结算实现。

| 模块 | 已实现 | 当前限制 |
| --- | --- | --- |
| `game-core` | 2–6 人 Kuhn、两人 Leduc、2–8 人标准无限注德州；4–8 人足额 live straddle；整数筹码、短全下重开、边池与返还；至多七名对手的范围 equity | 无抽水／re-straddle／多次发牌；短 straddle 拒绝；不是完整蘑菇会话或 WPK 规则映射 |
| `strategy` | CFR、external-sampling MCCFR、线性迭代权重、可恢复 RNG；严格 JSON 工件 | 表格规模有限，尚无完整六人策略库 |
| `search` | 有限博弈公共全范围根、隐蔽多后续策略、精确或抽样联合收益 rollout、预算拒绝 | 八人抽样搜索已用合成信念验证；真实公共根自动重建和后续策略覆盖未完成 |
| `evaluation` | 信息集一致最佳响应、NashConv、双人零和 exploitability、成对抽样比较 | 多人不报告双人意义的可剥削度 |
| `opponent` | 去重公开事件、机会分子／分母、Beta／Dirichlet 后验、时间截止、范围 Bayes、有限权重专家混合 | 风格不是唯一底牌范围，也不是行动 EV |
| `mushroom` | 费用前扣、主池独赢奖励、待退缴费跟踪、盲位顺延、原额退款、实际筹码续局；同一 Pluribus 训练器支持有限视野跨手模型 | 视野末尾退款是训练近似；re-straddle、长期延续价值、成熟策略与实局推荐未完成；旧冻结／均分仅为历史模拟配置 |
| `analysis` | SQLite 会话、训练队列与取消、blueprint 注册、有限搜索与牌力 API；独立进程空闲自博弈、单一检查点自动续训 | 自动候选是有资源上限的有限视野研究模型；实局匹配、远程租户／生产部署未实现 |
| `extension` | 自动核心训练状态、折叠的算法验证工具、策略库管理、假设画像适应实验 | 已移除模拟与牌力入口；WPK 采集由独立 TGTO-FronEnd 承担 |

## 当前开发用本地 API

新增 WS `/v1/research/stream` 接收独立 TGTO 网络元数据，本地默认校验 Chrome 扩展 Origin 并自动连接，远端令牌模式保留首消息鉴权；持久化与能力协商见 [对接说明](tgto-integration.md)。它不创建模拟牌局，也不复用 HTTP schema。

默认本地 HTTP 路由要求自动发送的固定 `X-Texas-Local: 1` 标记及合法 Host/Origin，无需令牌；显式设置 TEXAS_API_TOKEN 时保留 Bearer 模式。`GET /v1/health` 只公开服务元数据；`research_ready` 表示研究接口可用，不表示所有信息集已有训练策略。

| 方法 / 路径 | 用途 |
| --- | --- |
| `POST /v1/sessions` | 以规则配置及 seed 创建模拟手牌 |
| `GET /v1/sessions/{id}?viewer=N` | 从事件重建指定座位可见状态；省略 viewer 展示当前行动者 |
| `POST /v1/sessions/{id}/actions` | 提交 `action`、`event_id`、`expected_version` |
| `GET /v1/sessions/{id}/profiles?as_of=N` | 只从此前已发生的公开事件重建会话画像 |
| `DELETE /v1/sessions/{id}` | 删除该模拟手牌及事件、幂等记录 |
| `POST /v1/training` | 小型异步训练任务，立即返回 job |
| `GET /v1/core-training` | 自动核心训练阶段、已保存进度及明确的研究质量标签 |
| `GET /v1/training/{id}` | 实际完成轮数、停止原因、独立评测与工件 ID |
| `DELETE /v1/training/{id}` | 请求合作式取消，保留已完成迭代 |
| `GET /v1/blueprints` | 列出本机可校验工件，不接受任意文件路径 |
| `POST /v1/analyze` | 对匹配的会话版本和 blueprint 查询频率或有限搜索 |
| `POST /v1/equity` | 指定对手数与可选加权范围的 equity；不提供范围时均匀对手 |
| `POST /v1/hand-strength` | 当前牌型、理论／阻断／指定范围 nuts、领先平落权重和击败组合 |
| `POST /v1/experiments/adaptation` | 明确假设风格的有限博弈适应实验；独立评估收益及最坏损失 |

HTTP 请求／响应采用 snake_case。共享响应契约位于 `packages/protocol/src/index.ts`，生成 JSON Schema；Python 边界用 Pydantic 严格验证，接口测试检查跨语言一致性。健康／分析响应的 schema 版本为 `1.1`。牌型模块另外携带自身规则／模型版本。

`GameConfig` 包含 `game_id`、`num_players`、`stack`、可选逐席 `stacks`、`small_blind`、`big_blind`、可选 `straddle`、`button`、`abstraction`。逐席筹码用于 Holdem 与 mushroom，提供时覆盖统一 `stack`；标准 Holdem 的足额 live straddle 支持 4–8 人，位于大盲左侧并保留加注权。各规则的真实版本由引擎计算；无关配置不会冒充某个博弈已支持的功能。Holdem 抽象、筹码与 straddle 和规则版本绑定，不能用不同版本的工件查询。

蘑菇有限视野模型固定 2/4/8，并额外使用 `session_hands`。`abstraction=mushroom_context` 只允许用于蘑菇局，CLI／Python／Zod／JSON Schema 保持一致；可复用信息键和真实下注翻译见[上下文模型](mushroom-context.md)。该 API 仍为显式模拟会话，不能把采集状态填入随机发牌会话以冒充实局分析。

请求最大 256 KiB，同时检查 Content-Length 和实际流字节数。允许的 Origin 必须精确匹配；未知 Origin、令牌缺失、非法输入、版本冲突、队列满分别返回 403／401／422／409／429。服务仅绑定 loopback。默认不生成令牌文件，SQLite／工件位于被忽略的 `artifacts/local`，可用 `TEXAS_DATA_DIR` 指定隔离目录。当前存储仅为模拟数据。

## 事件、并发与恢复

SQLite 在一个事务内检查版本、追加行动及自动机会事件、保存幂等响应。同 `event_id` 和同内容重复请求返回原响应；同 ID 内容冲突或旧版本拒绝。失败动作不改变状态。发牌使用规则给出的概率和可复现种子；存档包含明确机会事件，重启无需猜测 RNG 进度。对外观察不暴露其他座位未公开底牌。

`state_version` 是已应用事件数，包括机会发牌。任何分析前后都核对版本；新动作清除旧结果，迟到响应不得覆盖新局面。画像按 `as_of` 事件前缀重放，Holdem VPIP/PFR 只有在完整手牌结束后才成为可用统计，不把未来摊牌或结算带入早期决策。

手工研究训练使用单线程 worker，最多 4 个排队／运行任务，HTTP 作业最多 100,000 轮、2,000,000 遍历节点和 20 秒训练预算。独立评测另有 100,000 节点／3 秒预算。取消和中断只保存完整迭代；服务重启后旧手工作业标记取消，已写入的 checkpoint 可通过 CLI 续训。

核心自动训练使用 `spawn` 独立进程，默认八人 800 分蘑菇两手模型，以同一个 `core/training.sqlite3` 保存全部席位的训练状态。业务请求、采集事件和手工作业使其暂停；健康／状态轮询与心跳不计忙碌。空闲 30 秒恢复正在遍历的栈，服务重启从完整检查点恢复 RNG 与遗憾。进程文件锁防止重复写入，策略表、轮数与 RNG 同事务提交，失败恢复最后完整迭代；SQLite 页缓存与全表操作内存有界，本轮工作字典仍受进程内存上限约束。资源限额、移植与导入导出见 [训练入口](../training/README.md)。自动候选未晋级为实局策略，不能用该调度交付替代抽象、覆盖和独立质量评测。

搜索／equity 使用至多两个计算槽；搜索最多 10 秒。根枚举、训练与叶 rollout 各有节点上限，共享墙钟截止时间。单个规则／牌型函数不做强制进程抢占；这些预算是合作式计算预算。大规模训练应使用 CLI 独立进程和操作系统资源约束，不从侧栏启动。

Python 搜索入口新增 `sampled_rollouts=True`，与 `algorithm="mccfr"` 配合，将选定后续策略的完整轨迹作为机会节点抽样。支持现有八人有限视野蘑菇模型；每条样本保留筹码、蘑菇池、后继手和终局退款的收益口径。随机数属于训练器，取消未完成迭代后重试不偷换随机流；抽样与精确尾值的规则标识不同。诊断记录 `continuation_evaluation`、实际轨迹数和节点数，未输出未经估计的行动 EV 或误差界。当前这一入口在离线引擎调用，HTTP 与采集链路尚无可信的八人公共信念生成器，不能因此宣称已经推送实局建议。

画像适应实验使用明确标注的合成 fold／call／raise 偏置模型，样本数是调用者设定的门槛条件，不冒充实际观测或校准置信度。其模型增益是整场有限博弈的期望净收益，不能当作当前牌面行动 EV。只有双人零和可精确验证最坏情况约束；多人／覆盖不足／预算耗尽保留基线。

未训练信息集返回空动作和具体原因。搜索因预算／覆盖失败时只回退到已经覆盖该信息集的 blueprint，并写明来源和警告。只展示实际计算的指标；不将频率、equity、画像标签或随机基线包装成最优 EV。

## 浏览器与视觉

当前构建产物为离线研究版 Chrome MV3 侧栏，使用 `setPanelBehavior` 打开，目前不注入站点脚本；目标生产版本将按已验证的数据来源增加 WPK 采集适配器。只请求 sidePanel 和本机 HTTP host 权限。本地界面无令牌输入并自动连接；固定本机地址、有限请求超时、AbortController 和状态版本守卫共同防止旧结果继续展示。

[DESIGN.md](../DESIGN.md) 保留用户指定参考；实现采用深紫 `#4a154b`、奶油／淡紫表面和胶囊按钮，适配窄侧栏。字体全部为本机字体栈。真实浏览器工作台验证与正式 Chrome 扩展安装验收分别记录，构建成功不能替代后者。
