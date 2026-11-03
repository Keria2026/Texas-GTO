import { useCallback, useEffect, useRef, useState } from "react";
import {
  BlueprintsResponseSchema, HealthResponseSchema, TrainingJobSchema, AdaptationResponseSchema,
  CoreTrainingResponseSchema,
} from "@texas-gto/protocol";
import type {
  BlueprintsResponse, GameConfig, GameId, HealthResponse, TrainingJob, AdaptationResponse,
  CoreTrainingResponse,
} from "@texas-gto/protocol";

const API = "http://127.0.0.1:8765";
type Schema<T> = { parse: (value: unknown) => T };
class Cancelled extends Error {}
const jobNames = { queued: "排队中", running: "自博弈进行中", completed: "已完成", cancelled: "已取消", failed: "训练失败" };
const coreNames: Record<CoreTrainingResponse["phase"], string> = {
  disabled: "自动训练已关闭", starting: "恢复训练进度", waiting_for_idle: "等待服务空闲",
  training: "自博弈进行中", paused: "已暂停，等待空闲", saving: "正在保存进度",
  limited: "已达到资源上限", error: "训练遇到问题", stopped: "训练已停止",
};
const gameConfig = (game: GameId, players: number): GameConfig => ({
  game_id: game, num_players: game === "leduc" ? 2 : players,
  stack: 20, small_blind: 1, big_blind: 2, button: 0, abstraction: "none",
});
const stringify = (value: unknown) => value === null || value === undefined ? "—" : typeof value === "string" ? value : JSON.stringify(value);
const percent = (value: number) => `${(value * 100).toFixed(1)}%`;

export function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [core, setCore] = useState<CoreTrainingResponse | null>(null);
  const [coreError, setCoreError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const requests = useRef(new Map<string, AbortController>());
  const connected = useRef(false);
  const [seed, setSeed] = useState(0);
  const [trainingGame, setTrainingGame] = useState<"kuhn" | "leduc">("kuhn");
  const [trainingPlayers, setTrainingPlayers] = useState(2);
  const [algorithm, setAlgorithm] = useState<"cfr" | "mccfr" | "pluribus">("pluribus");
  const [iterations, setIterations] = useState(1000);
  const [job, setJob] = useState<TrainingJob | null>(null);
  const [blueprints, setBlueprints] = useState<BlueprintsResponse["blueprints"]>([]);
  const [blueprint, setBlueprint] = useState("");
  const [opponentMode, setOpponentMode] = useState<"call" | "raise" | "fold">("call");
  const [adaptationSamples, setAdaptationSamples] = useState(100);
  const [adaptation, setAdaptation] = useState<AdaptationResponse | null>(null);

  useEffect(() => {
    const active = requests.current;
    return () => { for (const controller of active.values()) controller.abort(); };
  }, []);

  const request = useCallback(async <T,>(key: string, path: string, schema: Schema<T>, body?: unknown, method = body === undefined ? "GET" : "POST"): Promise<T> => {
    requests.current.get(key)?.abort();
    const controller = new AbortController();
    requests.current.set(key, controller);
    setBusy((current) => new Set(current).add(key));
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
    try {
      const response = await fetch(`${API}${path}`, {
        method, signal: controller.signal, cache: "no-store", credentials: "omit",
        referrerPolicy: "no-referrer",
        headers: { ...(body === undefined ? {} : { "Content-Type": "application/json" }), "X-Texas-Local": "1" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const payload: unknown = await response.json();
      if (requests.current.get(key) !== controller || controller.signal.aborted) throw new Cancelled();
      if (!response.ok) {
        const detail = typeof payload === "object" && payload !== null && "detail" in payload && typeof payload.detail === "string" ? payload.detail : `服务返回 HTTP ${response.status}`;
        throw new Error(detail);
      }
      return schema.parse(payload);
    } catch (cause) {
      if (timedOut) throw new Error("请求超过 15 秒，已取消等待。请确认服务状态后重试。");
      if (controller.signal.aborted || cause instanceof Cancelled) throw new Cancelled();
      if (cause instanceof Error && cause.name === "ZodError") throw new Error("服务响应不符合 1.1 协议，已停止显示结果。请更新本地服务与扩展。");
      if (cause instanceof TypeError) throw new Error("无法连接本地服务，请确认服务已启动并允许当前界面的 Origin。");
      throw cause;
    } finally {
      window.clearTimeout(timeout);
      if (requests.current.get(key) === controller) {
        requests.current.delete(key);
        setBusy((current) => { const next = new Set(current); next.delete(key); return next; });
      }
    }
  }, []);

  const report = useCallback((cause: unknown) => {
    if (!(cause instanceof Cancelled)) setError(cause instanceof Error ? cause.message : "请求失败，请稍后重试。");
  }, []);
  const clearAdaptation = () => { requests.current.get("adaptation")?.abort(); setAdaptation(null); };

  async function connect() {
    setError(null); setHealth(null); clearAdaptation();
    try {
      setHealth(await request("health", "/v1/health", HealthResponseSchema, undefined, "GET"));
      await refreshBlueprints();
    } catch (cause) { report(cause); }
  }

  async function refreshBlueprints() {
    const value = await request("blueprints", "/v1/blueprints", BlueprintsResponseSchema);
    clearAdaptation();
    setBlueprints(value.blueprints);
    setBlueprint((current) => value.blueprints.some((item) => item.blueprint_id === current) ? current : value.blueprints[0]?.blueprint_id ?? "");
  }

  async function train() {
    setError(null);
    try {
      const value = await request("training", "/v1/training", TrainingJobSchema, {
        game: gameConfig(trainingGame, trainingPlayers), algorithm, weighting: "linear",
        iterations, seed, max_nodes: 2000000, deadline_seconds: 10,
      });
      setJob(value);
      if (value.status === "completed") await refreshBlueprints();
    } catch (cause) { report(cause); }
  }

  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    const jobId = job.job_id;
    let active = true;
    let timer: number;
    async function poll() {
      try {
        const value = await request("poll", `/v1/training/${encodeURIComponent(jobId)}`, TrainingJobSchema);
        if (!active) return;
        if (value.status === "completed") {
          const list = await request("blueprints", "/v1/blueprints", BlueprintsResponseSchema);
          if (active) {
            requests.current.get("adaptation")?.abort(); setAdaptation(null);
            setBlueprints(list.blueprints); setBlueprint(value.blueprint_id ?? ""); setJob(value);
          }
          return;
        }
        setJob(value);
        if (value.status === "failed") { setError(value.error ?? "训练失败，请检查预算与参数。"); return; }
        if (value.status === "cancelled") return;
        timer = window.setTimeout(() => void poll(), 750);
      } catch (cause) {
        if (active && !(cause instanceof Cancelled)) { report(cause); timer = window.setTimeout(() => void poll(), 3000); }
      }
    }
    timer = window.setTimeout(() => void poll(), 750);
    return () => { active = false; window.clearTimeout(timer); requests.current.get("poll")?.abort(); };
  }, [job?.job_id, job?.status, request, report]);

  async function cancelTraining() {
    if (!job) return;
    setError(null);
    try { setJob(await request("training", `/v1/training/${encodeURIComponent(job.job_id)}`, TrainingJobSchema, undefined, "DELETE")); }
    catch (cause) { report(cause); }
  }

  async function runAdaptation() {
    if (!blueprint) return;
    setError(null); clearAdaptation();
    try {
      const value = await request("adaptation", "/v1/experiments/adaptation", AdaptationResponseSchema, {
        blueprint_id: blueprint, hero: 0, opponent_mode: opponentMode,
        sample_count: adaptationSamples, max_worst_case_loss: 0.04,
      });
      if (value.blueprint_id !== blueprint || value.opponent_mode !== opponentMode) throw new Error("实验返回的工件或假设风格不一致，已拒绝显示。");
      setAdaptation(value);
    } catch (cause) { report(cause); }
  }

  useEffect(() => {
    let live = true;
    const check = async () => {
      if (requests.current.has("health")) return;
      try {
        const value = await request("health", "/v1/health", HealthResponseSchema);
        if (!live) return;
        setHealth(value);
        if (!connected.current) {
          connected.current = true;
          await refreshBlueprints();
          if (live) setError(null);
        }
      } catch (cause) {
        if (live && !(cause instanceof Cancelled)) {
          connected.current = false;
          setHealth(null); setBlueprints([]); setBlueprint(""); setAdaptation(null);
        }
      }
    };
    void check();
    const timer = window.setInterval(() => void check(), 3000);
    return () => { live = false; window.clearInterval(timer); };
  }, [request]);

  const unlocked = health !== null;

  useEffect(() => {
    if (!unlocked) { setCore(null); setCoreError(null); return; }
    let active = true;
    let timer: number;
    async function poll() {
      try {
        const value = await request("core", "/v1/core-training", CoreTrainingResponseSchema);
        if (active) { setCore(value); setCoreError(null); }
      } catch (cause) {
        if (active && !(cause instanceof Cancelled)) {
          setCore(null);
          setCoreError(cause instanceof Error ? cause.message : "自动训练状态读取失败");
        }
      } finally {
        if (active) timer = window.setTimeout(() => void poll(), 3000);
      }
    }
    void poll();
    return () => { active = false; window.clearTimeout(timer); requests.current.get("core")?.abort(); };
  }, [unlocked, request]);

  const training = !!job && ["queued", "running"].includes(job.status);
  const selectedBlueprint = blueprints.find((item) => item.blueprint_id === blueprint);
  const adaptationSupported = selectedBlueprint && ["kuhn", "leduc"].includes(String(selectedBlueprint.metadata.game_id)) && selectedBlueprint.metadata.num_players === 2;

  return (
    <div className="workspace">
      <header className="app-header"><div className="brand">Texas GTO</div><span className="version">训练管理 · 0.1</span></header>
      <main>
        <section className="intro"><h1>训练与策略库</h1><p>目标玩法：4–8 人德州·蘑菇，以 8 人为主。</p><p>服务空闲时自动训练，持续保存同一份核心进度。目标是长期期望净积分最大化。</p></section>
        <section className="connection" aria-label="本地分析服务">
          <div className="row"><div><h2>连接研究引擎</h2><p className="small" role="status">{busy.has("health") ? "正在检查…" : health ? `服务可用 · 协议 ${health.schema_version}` : "正在等待本地服务"}</p></div><button className="secondary" disabled={busy.has("health")} onClick={() => void connect()}>检查连接</button></div>
          <p className="small">打开后自动连接 127.0.0.1:8765。服务离线时会自动重试。</p>
        </section>
        {error && <div className="error-box" role="alert"><span>{error}</span><button aria-label="关闭错误提示" onClick={() => setError(null)}>关闭</button></div>}
        <section className="panel core-training" aria-labelledby="core-title">
          <div className="row"><h2 id="core-title">核心策略自动训练</h2><span className="badge" role="status">{core ? coreNames[core.phase] : coreError ? "状态暂不可用" : unlocked ? "读取训练状态" : "等待连接"}</span></div>
          <p>{core?.enabled === false ? "服务器已关闭自动训练。需要在服务配置中启用后才能继续。" : "无需手动开始。牌局事件或计算请求到来时暂停，服务空闲 30 秒后继续；关闭此页面不影响训练。"}</p>
          {core && <>
            <p>当前模型：{core.game.abstraction === "mushroom_population"
              ? "8 个模型席位 · 4–8 人开局 · 不同筹码与蘑菇权益"
              : `${core.game.num_players} 人 · 每席初始 ${core.game.stack} 分`} · Pluribus MCCFR</p>
            {core.game.abstraction === "mushroom_population" && <p className="note">累计轮数和信息集包含保留的旧规则数据；当前规则迭代另列，策略覆盖仍需单独核验。</p>}
            <dl className="experiment-metrics">
              <div><dt>已保存迭代</dt><dd>{core.iterations.toLocaleString("zh-CN")}</dd></div>
              {core.rule_iterations != null && <div><dt>当前规则迭代（前注 {core.game.ante ?? 0} 分）</dt><dd>{core.rule_iterations.toLocaleString("zh-CN")}</dd></div>}
              {core.training_roots != null && <div><dt>训练开局／其中来自实采</dt><dd>{core.training_roots.toLocaleString("zh-CN")} / {(core.observed_openings ?? 0).toLocaleString("zh-CN")}</dd></div>}
              <div><dt>累计训练信息集</dt><dd>{core.information_sets.toLocaleString("zh-CN")}</dd></div>
              <div><dt>已积累平均策略的信息集</dt><dd>{core.averaged_information_sets.toLocaleString("zh-CN")}</dd></div>
              <div><dt>保存的训练进度</dt><dd>{core.checkpoint_bytes ? `${(core.checkpoint_bytes / 1048576).toFixed(1)} MiB` : "等待首次保存"}</dd></div>
            </dl>
            <p className="note">当前采用 {core.game.session_hands} 手训练视野，仍是研究近似。新增实采开局不等于策略已覆盖；行动推荐仍须通过当前节点的输入、策略与反馈检查。</p>
            {core.opening_capacity_reached && <p className="error" role="status">实采开局已达到容量上限，新增组合暂未纳入训练，已有累计保持保留。</p>}
            {core.iterations > 0 && core.averaged_information_sets === 0 && <p className="small">尚未到平均策略积累周期；已训练的信息集数量不代表实局策略覆盖。</p>}
            {core.reason && <details><summary>查看训练诊断</summary><p>{core.phase === "limited" ? "已达到资源上限，最近保存的进度仍然保留。" : "训练已停止，不会自动重置已保存的进度。"}</p><pre>{core.reason}</pre></details>}
          </>}
          {coreError && <p className="error" role="alert">{coreError} 正在自动重试。</p>}
        </section>
        <details className="panel research-tools" aria-labelledby="training-title">
          <summary id="training-title">算法验证（开发工具）</summary>
          <div className="section-heading"><p>Kuhn 与 Leduc 是数学基准环境，训练结果仅适用于对应环境，不能用于德州·蘑菇决策。</p></div>
          <div className="form-grid">
            <label>验证环境<select value={trainingGame} onChange={(event) => setTrainingGame(event.target.value as "kuhn" | "leduc")}><option value="kuhn">Kuhn（算法校验）</option><option value="leduc">Leduc（算法校验）</option></select></label>
            <label>验证人数<select value={trainingGame === "leduc" ? 2 : trainingPlayers} disabled={trainingGame === "leduc"} onChange={(event) => setTrainingPlayers(Number(event.target.value))}>{(trainingGame === "leduc" ? [2] : [2, 3, 4, 5, 6]).map((count) => <option key={count} value={count}>{count} 人</option>)}</select></label>
            <label>算法<select value={algorithm} onChange={(event) => setAlgorithm(event.target.value as "cfr" | "mccfr" | "pluribus")}><option value="pluribus">Pluribus MCCFR（主线）</option><option value="mccfr">MCCFR（采样对照）</option><option value="cfr">全树 CFR（基准）</option></select></label>
            <label>迭代次数<input type="number" min={1} max={100000} value={iterations} onChange={(event) => setIterations(Number(event.target.value))} /></label>
            <label>随机种子<input type="number" min={0} max={4294967295} value={seed} onChange={(event) => setSeed(Number(event.target.value))} /></label>
          </div>
          <p className="small">{algorithm === "pluribus" ? "论文主线：采样自博弈，包含剪枝、折扣和分阶段策略积累。" : algorithm === "mccfr" ? "采样对照：抽样遍历博弈树，降低每轮计算量。" : "全树基准：遍历完整博弈树，适用于小型验证环境。"}</p>
          <p className="small">Leduc 固定为 2 人；Kuhn 验证引擎支持 2–6 人。每次训练预算 10 秒 / 200 万节点。</p>
          <button className="primary full" disabled={!unlocked || training || busy.has("training") || !Number.isInteger(iterations) || iterations < 1 || iterations > 100000 || !Number.isInteger(seed) || seed < 0 || seed > 4294967295} onClick={() => void train()}>{training ? "自博弈进行中…" : "运行算法验证训练"}</button>
          {job && <div className="job-result" role="status"><div className="row"><strong>{jobNames[job.status]}</strong>{training && <button className="text-button" disabled={busy.has("training")} onClick={() => void cancelTraining()}>取消训练</button>}</div>{job.error && <p className="error">{job.error}</p>}<details><summary>预算诊断与评测</summary><pre>{JSON.stringify({ diagnostics: job.diagnostics, evaluation: job.evaluation }, null, 2)}</pre></details></div>}
        </details>
        <section className="research-card" aria-labelledby="blueprints-title">
          <div className="row"><h2 id="blueprints-title">算法验证策略库</h2><button className="text-button" disabled={!unlocked || busy.has("blueprints")} onClick={() => { setError(null); void refreshBlueprints().catch(report); }}>刷新策略库</button></div>
          <label>策略库工件<select value={blueprint} onChange={(event) => { clearAdaptation(); setBlueprint(event.target.value); }}><option value="">选择已完成的训练工件</option>{blueprints.map((item) => <option key={item.blueprint_id} value={item.blueprint_id}>{stringify(item.metadata.game_id)} · {stringify(item.metadata.num_players)} 人 · {item.information_sets} 个信息集 · {item.blueprint_id.slice(0, 8)}</option>)}</select></label>
          {selectedBlueprint ? <dl className="experiment-metrics">
            <div><dt>训练环境</dt><dd>{stringify(selectedBlueprint.metadata.game_id)}</dd></div>
            <div><dt>玩家人数</dt><dd>{stringify(selectedBlueprint.metadata.num_players)}</dd></div>
            <div><dt>算法</dt><dd>{stringify(selectedBlueprint.metadata.algorithm)}</dd></div>
            <div><dt>已完成迭代</dt><dd>{stringify(selectedBlueprint.metadata.iterations)}</dd></div>
            <div><dt>已保存信息集</dt><dd>{selectedBlueprint.information_sets}</dd></div>
          </dl> : <p className="small" role="status">连接服务后可读取已有策略库，或运行算法验证训练生成策略库。</p>}
          <p className="small">一个策略库保存同一训练任务中各信息集的行动分布。信息集数量表示已保存的决策情景数量，不是独立模型数量，也不是训练样本数。</p>
          <p className="small">WPK 实时状态的策略推荐尚未接入；当前策略库用于离线算法验证。</p>
        </section>
          <details className="research-card adaptation-panel">
            <summary>画像适应实验</summary>
            <p>对选定的两人 Kuhn / Leduc blueprint，评估假设风格下的整局策略调整。此实验使用合成行为模型，不使用真实玩家画像。</p>
            <div className="form-grid">
              <label>假设风格<select value={opponentMode} onChange={(event) => { clearAdaptation(); setOpponentMode(event.target.value as "call" | "raise" | "fold"); }}>
                <option value="call">偏向跟注</option><option value="raise">偏向加注</option><option value="fold">偏向弃牌</option>
              </select></label>
              <label>假设样本数<input type="number" min={0} max={1000000} value={adaptationSamples} onChange={(event) => { clearAdaptation(); setAdaptationSamples(Number(event.target.value)); }} /></label>
            </div>
            <p className="small">Hero 为席位 0；相对基线的最坏收益损失上限为 0.04。假设样本数只是实验门槛，不代表已观察的数据或校准置信度。</p>
            {!adaptationSupported && <p className="small note">请先选择两人 Kuhn 或 Leduc 的训练工件。</p>}
            <button className="secondary full" disabled={!unlocked || !adaptationSupported || busy.has("adaptation") || adaptationSamples < 0 || adaptationSamples > 1000000} onClick={() => void runAdaptation()}>{busy.has("adaptation") ? "评估假设模型中…" : "运行画像适应实验"}</button>
            {adaptation && <div className="adaptation-result" aria-live="polite">
              <span className="badge">{adaptation.result.accepted ? "候选通过约束" : "保留基础策略"}</span>
              <dl className="experiment-metrics">
                <div><dt>调整混合权重</dt><dd>{percent(adaptation.result.selected_weight)}</dd></div>
                <div><dt>假设模型下整局收益增益</dt><dd>{adaptation.result.model_gain.toFixed(4)}</dd></div>
                <div><dt>基础策略最坏收益</dt><dd>{adaptation.result.baseline_worst_case?.toFixed(4) ?? "未评测"}</dd></div>
                <div><dt>候选策略最坏收益</dt><dd>{adaptation.result.candidate_worst_case?.toFixed(4) ?? "未评测"}</dd></div>
              </dl>
              <p className="small">判定原因：{adaptation.result.reason}</p>
              <p className="small note">以上为整个有限博弈的净收益，不是当前节点 EV 或实时行动建议。约束通过不保证对所有真实对手都有收益提升。</p>
            </div>}
          </details>
      </main>
      <footer>可复现的训练 · 明确的适用范围 · 可检查的结果</footer>
    </div>
  );
}
