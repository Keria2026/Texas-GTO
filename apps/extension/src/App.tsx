import { useCallback, useEffect, useRef, useState } from "react";
import {
  AnalysisResponseSchema, BlueprintsResponseSchema, EquityResponseSchema,
  HealthResponseSchema, SessionResponseSchema, TrainingJobSchema,
  ProfilesResponseSchema,
  HandStrengthResponseSchema, AdaptationResponseSchema,
} from "@texas-gto/protocol";
import type {
  AnalysisResponse, BlueprintsResponse, EquityResponse, GameConfig, GameId,
  HealthResponse, SessionResponse, TrainingJob,
  ProfilesResponse,
  HandStrengthResponse, AdaptationResponse,
} from "@texas-gto/protocol";

const API = "http://127.0.0.1:8765";
const sections = { hand: "牌局", strategy: "训练 / 搜索", equity: "牌力", players: "玩家" } as const;
type Section = keyof typeof sections;
type Schema<T> = { parse: (value: unknown) => T };
class Cancelled extends Error {}

const gameNames: Record<GameId, string> = { kuhn: "Kuhn 小型博弈", leduc: "Leduc 两街博弈", holdem: "有限动作德州" };
const jobNames = { queued: "排队中", running: "自博弈进行中", completed: "已完成", cancelled: "已取消", failed: "训练失败" };
const gameConfig = (game: GameId, players: number): GameConfig => ({
  game_id: game, num_players: game === "leduc" ? 2 : players,
  stack: 20, small_blind: 1, big_blind: 2, button: 0, abstraction: "none",
});
const stringify = (value: unknown) => value === null || value === undefined ? "—" : typeof value === "string" ? value : JSON.stringify(value);
const actionName = (action: string) => ({ check: "过牌", bet: "下注", fold: "弃牌", call: "跟注", raise: "加注" }[action] ?? action.replace("raise_to:", "加注到 "));
const percent = (value: number) => `${(value * 100).toFixed(1)}%`;
const categoryName = (category: string) => ({
  high_card: "高牌", one_pair: "一对", two_pair: "两对", three_of_a_kind: "三条",
  straight: "顺子", flush: "同花", full_house: "葫芦", four_of_a_kind: "四条", straight_flush: "同花顺",
}[category] ?? category);
const warningText = (warning: string) => ({
  "Finite-budget research strategy; no global optimality guarantee": "有限预算下的研究策略，尚无全局最优保证。",
  "Finite continuation set and local re-solving do not guarantee a safe global equilibrium.": "有限后续策略与局部重求解，不能保证得到安全的全局均衡。",
  "Continuation biases preserve blueprint support and cannot revive absent actions.": "后续策略保留基础策略的动作支持，不能恢复其中概率为零的动作。",
  "Full Holdem public-root enumeration unsupported; using covered blueprint": "暂不支持完整德州公共根节点枚举，当前使用已覆盖的基础策略。",
}[warning] ?? warning);

function Cards({ value, game }: { value: unknown; game: GameId }) {
  const cards = Array.isArray(value) ? value : value === undefined || value === null ? [] : [value];
  return <div className="cards">{cards.length ? cards.map((card, index) => {
    const raw = stringify(card);
    const label = game === "leduc" && typeof card === "number" ? ["J", "Q", "K"][card] ?? raw : raw.replace(/s$/, "♠").replace(/h$/, "♥").replace(/d$/, "♦").replace(/c$/, "♣");
    return <span className={`playing-card ${/[hd]$/.test(raw) ? "red" : ""}`} key={index}>{label}</span>;
  }) : <span className="muted small">尚未发出</span>}</div>;
}

function CurrentHandStrength({ result }: { result: HandStrengthResponse }) {
  const comparison = result.opponents;
  return <div className="research-card hand-strength-result" aria-live="polite">
    <h3>当前五张牌型</h3>
    <strong>{result.current_category ? categoryName(result.current_category) : "未定义（翻前）"}</strong>
    {result.nuts ? <>
      <div className="metrics">
        <div><span>理论 nuts</span><strong>{result.nuts.theoretical.hero_is_nuts ? "是" : "否"}</strong></div>
        <div><span>计入我的阻断牌</span><strong>{result.nuts.blocker_aware.hero_is_nuts ? "是" : "否"}</strong></div>
      </div>
      <p className="small">理论 nuts 只排除公共牌；阻断分析额外排除我的底牌。</p>
    </> : <p className="small">翻前不足五张牌，当前牌型、nuts 与领先分布均未定义。</p>}
    {comparison && <>
      <p className="small">当前对单个均匀合法对手的领先分布</p>
      <div className="comparison-grid">
        <span>领先<strong>{percent(comparison.current_ahead_weight)}</strong></span>
        <span>平局<strong>{percent(comparison.current_tied_weight)}</strong></span>
        <span>落后<strong>{percent(comparison.current_behind_weight)}</strong></span>
      </div>
      <p className="small">可击败我的组合共 {comparison.beating_combinations_count} 个{comparison.beating_combinations_truncated ? "，最多展示 5 个" : ""}。</p>
      {comparison.beating_combinations.map((combo) => <div className="beating-combo" key={combo.cards.join("")}>
        <Cards value={combo.cards} game="holdem" /><span className="small">{categoryName(combo.category)}</span>
      </div>)}
    </>}
    {result.warnings.map((warning, index) => <p className="small note" key={index}>{warning}</p>)}
  </div>;
}

export function App() {
  const [section, setSection] = useState<Section>("hand");
  const [token, setToken] = useState("");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());
  const requests = useRef(new Map<string, AbortController>());
  const [game, setGame] = useState<GameId>("kuhn");
  const [players, setPlayers] = useState(2);
  const [seed, setSeed] = useState(0);
  const [session, setSession] = useState<SessionResponse | null>(null);
  const sessionRef = useRef<SessionResponse | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [trainingGame, setTrainingGame] = useState<"kuhn" | "leduc">("kuhn");
  const [trainingPlayers, setTrainingPlayers] = useState(2);
  const [algorithm, setAlgorithm] = useState<"cfr" | "mccfr">("mccfr");
  const [iterations, setIterations] = useState(1000);
  const [job, setJob] = useState<TrainingJob | null>(null);
  const [blueprints, setBlueprints] = useState<BlueprintsResponse["blueprints"]>([]);
  const [blueprint, setBlueprint] = useState("");
  const [search, setSearch] = useState(false);
  const [depth, setDepth] = useState(1);
  const [hero, setHero] = useState("As Kd");
  const [board, setBoard] = useState("");
  const [opponents, setOpponents] = useState(1);
  const [samples, setSamples] = useState(1000);
  const [equity, setEquity] = useState<EquityResponse | null>(null);
  const [profiles, setProfiles] = useState<ProfilesResponse | null>(null);
  const [handStrength, setHandStrength] = useState<HandStrengthResponse | null>(null);
  const [opponentMode, setOpponentMode] = useState<"call" | "raise" | "fold">("call");
  const [adaptationSamples, setAdaptationSamples] = useState(100);
  const [adaptation, setAdaptation] = useState<AdaptationResponse | null>(null);

  useEffect(() => {
    const active = requests.current;
    return () => { for (const controller of active.values()) controller.abort(); };
  }, []);

  const request = useCallback(async <T,>(key: string, path: string, schema: Schema<T>, body?: unknown, method = body === undefined ? "GET" : "POST", authenticated = true): Promise<T> => {
    if (authenticated && !token.trim()) throw new Error("请输入服务启动时生成的本地访问令牌。");
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
        headers: { ...(body === undefined ? {} : { "Content-Type": "application/json" }), ...(authenticated ? { Authorization: `Bearer ${token.trim()}` } : {}) },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const payload: unknown = await response.json();
      if (requests.current.get(key) !== controller || controller.signal.aborted) throw new Cancelled();
      if (!response.ok) {
        const detail = typeof payload === "object" && payload !== null && "detail" in payload && typeof payload.detail === "string" ? payload.detail : `服务返回 HTTP ${response.status}`;
        throw new Error(response.status === 401 ? "访问令牌无效，请核对本地服务的启动输出。" : detail);
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
  }, [token]);

  const report = useCallback((cause: unknown) => {
    if (!(cause instanceof Cancelled)) {
      setAnalysis(null);
      setError(cause instanceof Error ? cause.message : "请求失败，请稍后重试。");
    }
  }, []);
  const clearAdaptation = () => { requests.current.get("adaptation")?.abort(); setAdaptation(null); };
  const clearAnalysis = () => { requests.current.get("analysis")?.abort(); setAnalysis(null); clearAdaptation(); };
  const clearEquity = () => {
    requests.current.get("equity")?.abort(); requests.current.get("strength")?.abort();
    setEquity(null); setHandStrength(null);
  };
  const acceptSession = (value: SessionResponse) => {
    requests.current.get("profiles")?.abort(); setProfiles(null);
    sessionRef.current = value; setSession(value); setUncertain(false); clearAnalysis();
  };

  function changeToken(value: string) {
    for (const controller of requests.current.values()) controller.abort();
    requests.current.clear(); setBusy(new Set());
    setToken(value); setSession(null); sessionRef.current = null;
    setAnalysis(null); setJob(null); setBlueprints([]); setBlueprint(""); setEquity(null); setProfiles(null); setError(null);
    setHandStrength(null); setAdaptation(null);
  }

  async function connect() {
    setError(null); setHealth(null); clearAnalysis();
    try {
      setHealth(await request("health", "/v1/health", HealthResponseSchema, undefined, "GET", false));
      if (token.trim()) await refreshBlueprints();
    } catch (cause) { report(cause); }
  }

  async function newSession() {
    setError(null); clearAnalysis();
    try {
      acceptSession(await request("session", "/v1/sessions", SessionResponseSchema, { game: gameConfig(game, players), seed }));
    } catch (cause) { report(cause); }
  }

  async function refreshSession() {
    const before = sessionRef.current;
    if (!before) return;
    setError(null); clearAnalysis();
    try {
      const value = await request("session", `/v1/sessions/${encodeURIComponent(before.session_id)}`, SessionResponseSchema);
      if (sessionRef.current?.session_id === before.session_id) acceptSession(value);
    } catch (cause) { report(cause); }
  }

  async function act(action: string) {
    const before = sessionRef.current;
    if (!before || busy.has("session") || uncertain) return;
    setError(null); clearAnalysis();
    try {
      const value = await request("session", `/v1/sessions/${encodeURIComponent(before.session_id)}/actions`, SessionResponseSchema, {
        action, event_id: crypto.randomUUID(), expected_version: before.state_version,
      });
      if (sessionRef.current?.session_id === before.session_id && sessionRef.current.state_version === before.state_version) acceptSession(value);
    } catch (cause) {
      if (!(cause instanceof Cancelled)) { setUncertain(true); report(cause); }
    }
  }

  async function refreshBlueprints() {
    const value = await request("blueprints", "/v1/blueprints", BlueprintsResponseSchema);
    clearAnalysis();
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
            requests.current.get("analysis")?.abort(); setAnalysis(null);
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

  async function analyze() {
    const before = sessionRef.current;
    if (!before || !blueprint || before.terminal || uncertain) return;
    setError(null); clearAnalysis();
    try {
      const value = await request("analysis", "/v1/analyze", AnalysisResponseSchema, {
        session_id: before.session_id, state_version: before.state_version, blueprint_id: blueprint,
        search, depth_limit: depth, iterations: 100, max_nodes: 200000, deadline_seconds: 3,
      });
      if (value.session_id !== sessionRef.current?.session_id || value.state_version !== sessionRef.current.state_version) return;
      if (value.rules_version !== before.rules_version || value.actions.some((item) => !before.legal_actions.includes(item.action))) throw new Error("策略与当前规则或合法动作不一致，已拒绝显示。");
      setAnalysis(value);
    } catch (cause) { report(cause); }
  }

  async function calculateEquity() {
    setError(null); clearEquity();
    const parseCards = (value: string) => value.trim() ? value.trim().split(/[\s,，]+/) : [];
    const heroCards = parseCards(hero), boardCards = parseCards(board);
    if (heroCards.length !== 2 || ![0, 3, 4, 5].includes(boardCards.length) || [...heroCards, ...boardCards].some((card) => !/^[2-9TJQKA][shdc]$/.test(card)) || new Set([...heroCards, ...boardCards]).size !== heroCards.length + boardCards.length) {
      setError("请输入 2 张底牌及 0 / 3 / 4 / 5 张公共牌，使用 As、Td 等格式，牌张不能重复。"); return;
    }
    const results = await Promise.allSettled([
      request("equity", "/v1/equity", EquityResponseSchema, { hero: heroCards, board: boardCards, opponents, samples, seed }).then(setEquity),
      request("strength", "/v1/hand-strength", HandStrengthResponseSchema, { hero: heroCards, board: boardCards, max_beating_combos: 5 }).then(setHandStrength),
    ]);
    for (const result of results) if (result.status === "rejected") report(result.reason);
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

  async function refreshProfiles() {
    const before = sessionRef.current;
    if (!before) return;
    setError(null); setProfiles(null);
    try {
      const value = await request("profiles", `/v1/sessions/${encodeURIComponent(before.session_id)}/profiles?as_of=${before.state_version}`, ProfilesResponseSchema);
      if (value.session_id === sessionRef.current?.session_id && value.state_version === sessionRef.current.state_version) setProfiles(value);
    } catch (cause) { report(cause); }
  }

  const unlocked = token.trim().length > 0;
  const training = !!job && ["queued", "running"].includes(job.status);
  const observation = session?.observation;
  const visibleAnalysis = analysis && session && analysis.session_id === session.session_id && analysis.state_version === session.state_version ? analysis : null;
  const shownBlueprints = blueprints;
  const selectedBlueprint = blueprints.find((item) => item.blueprint_id === blueprint);
  const adaptationSupported = selectedBlueprint && ["kuhn", "leduc"].includes(String(selectedBlueprint.metadata.game_id)) && selectedBlueprint.metadata.num_players === 2;

  return (
    <div className="workspace">
      <header className="app-header"><div className="brand"><span className="brand-mark" aria-hidden="true">♠</span>Texas GTO</div><span className="version">离线研究 · 0.1</span></header>
      <main>
        <section className="intro"><span className="eyebrow">PLURIBUS RESEARCH LAB</span><h1>训练、回放，<br />验证每个决策。</h1><p>自博弈基础策略 → 深度受限搜索。<br />从有限博弈检验方法，再拓展多人德州。</p><span className="badge">模拟数据 · 独立本地运行</span></section>
        <section className="connection" aria-label="本地分析服务">
          <div className="row"><div><h2>连接研究引擎</h2><p className="small" role="status">{busy.has("health") ? "正在检查…" : health ? `服务可用 · 协议 ${health.schema_version}` : "尚未连接本地服务"}</p></div><button className="secondary" disabled={busy.has("health")} onClick={() => void connect()}>检查连接</button></div>
          <label>访问令牌<input type="password" autoComplete="off" spellCheck={false} value={token} onChange={(event) => changeToken(event.target.value)} placeholder="粘贴本地服务启动时的令牌" /></label>
          <p className="small">令牌仅保留在当前界面内存。所有请求发送至 127.0.0.1:8765。</p>
        </section>
        {error && <div className="error-box" role="alert"><span>{error}</span><button aria-label="关闭错误提示" onClick={() => setError(null)}>×</button></div>}
        <nav className="tabs" aria-label="研究视图">{(Object.keys(sections) as Section[]).map((key) => <button key={key} aria-pressed={section === key} onClick={() => setSection(key)}>{sections[key]}</button>)}</nav>
        {section === "hand" && <section className="panel" aria-labelledby="hand-title">
          <div className="section-heading"><span className="eyebrow">01 / SIMULATE</span><h2 id="hand-title">建立一手可重复的牌局</h2><p>轮流操控各席；仅展示当前行动席位的底牌。</p></div>
          <div className="form-grid"><label className="wide">游戏<select value={game} onChange={(event) => { setGame(event.target.value as GameId); if (event.target.value === "leduc") setPlayers(2); }}>{Object.entries(gameNames).map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label><label>玩家人数<select value={players} disabled={game === "leduc"} onChange={(event) => setPlayers(Number(event.target.value))}>{[2, 3, 4, 5, 6].map((count) => <option key={count} value={count}>{count} 人</option>)}</select></label><label>随机种子<input type="number" min={0} max={2147483647} value={seed} onChange={(event) => { setSeed(Number(event.target.value)); clearEquity(); }} /></label></div>
          {game === "holdem" && <p className="small note">标准规则模拟 · 每席 20 筹码，盲注 1 / 2，按钮位 0，有限下注尺寸。尚无完整德州训练策略。</p>}
          <button className="primary full" disabled={!unlocked || busy.has("session")} onClick={() => void newSession()}>{busy.has("session") ? "处理中…" : session ? "创建新牌局" : "开始模拟"}</button>
          {session && observation && <div className="hand-card">
            <div className="row"><span className="badge">{session.terminal ? "本手已结束" : `席位 ${session.actor} 行动`}</span><span className="small muted">状态 v{session.state_version}</span></div>
            <div className="card-zone"><span className="eyebrow">{session.terminal ? "观察席位底牌" : "当前席位底牌"}</span><Cards game={session.game.game_id} value={observation.heroCards ?? observation.private_card} /><span className="eyebrow">公共牌</span><Cards game={session.game.game_id} value={observation.board ?? observation.public_card} /></div>
            <div className="metrics"><div><span>底池 / 投入</span><strong>{stringify(observation.pot ?? observation.contributions)}</strong></div><div><span>当前阶段</span><strong>{session.terminal ? "结算" : stringify(observation.street ?? (observation.round_index === undefined ? "单街" : `第 ${Number(observation.round_index) + 1} 街`))}</strong></div></div>
            {uncertain && <p className="error">上次请求的结果尚未确认。刷新状态后再行动。</p>}
            {!session.terminal && <div className="action-grid">{session.legal_actions.map((action) => <button className="secondary" key={action} disabled={uncertain || busy.has("session")} onClick={() => void act(action)}>{actionName(action)}</button>)}</div>}
            {session.payoffs && <div className="payoffs">{session.payoffs.map((payoff, player) => <span key={player}>席位 {player}<strong className={payoff > 0 ? "positive" : ""}>{payoff > 0 ? "+" : ""}{payoff}</strong></span>)}</div>}
            <div className="row"><button className="text-button" disabled={busy.has("session")} onClick={() => void refreshSession()}>刷新状态</button><button className="text-button" onClick={() => setSection("strategy")}>查看策略 →</button></div>
            <details><summary>公开行动与规则</summary><p className="small">{session.rules_version}</p><pre>{JSON.stringify(observation.public_history ?? observation.history ?? [], null, 2)}</pre></details>
          </div>}
        </section>}
        {section === "strategy" && <section className="panel" aria-labelledby="strategy-title">
          <div className="section-heading"><span className="eyebrow">02 / TRAIN & SEARCH</span><h2 id="strategy-title">先训练基础策略，再搜索</h2><p>有限博弈中的 Pluribus 方法验证台。训练预算与覆盖范围直接影响结果。</p></div>
          <div className="research-card"><h3>自博弈训练</h3><div className="form-grid"><label>训练游戏<select value={trainingGame} onChange={(event) => setTrainingGame(event.target.value as "kuhn" | "leduc")}><option value="kuhn">Kuhn</option><option value="leduc">Leduc</option></select></label><label>人数<select value={trainingGame === "leduc" ? 2 : trainingPlayers} disabled={trainingGame === "leduc"} onChange={(event) => setTrainingPlayers(Number(event.target.value))}><option value={2}>2 人</option><option value={3}>3 人</option></select></label><label>算法<select value={algorithm} onChange={(event) => setAlgorithm(event.target.value as "cfr" | "mccfr")}><option value="mccfr">MCCFR</option><option value="cfr">完整树 CFR</option></select></label><label>迭代次数<input type="number" min={1} max={100000} value={iterations} onChange={(event) => setIterations(Number(event.target.value))} /></label></div><p className="small">线性加权 · 10 秒 / 200 万节点预算 · 种子 {seed}</p><button className="primary full" disabled={!unlocked || training || busy.has("training") || iterations < 1 || iterations > 100000} onClick={() => void train()}>{training ? "自博弈进行中…" : "训练 blueprint"}</button>
            {job && <div className="job-result" role="status"><div className="row"><strong>{jobNames[job.status]}</strong>{training && <button className="text-button" disabled={busy.has("training")} onClick={() => void cancelTraining()}>取消训练</button>}</div>{job.error && <p className="error">{job.error}</p>}<details><summary>预算诊断与评测</summary><pre>{JSON.stringify({ diagnostics: job.diagnostics, evaluation: job.evaluation }, null, 2)}</pre></details></div>}
          </div>
          <div className="research-card"><div className="row"><h3>当前节点策略</h3><button className="text-button" disabled={!unlocked || busy.has("blueprints")} onClick={() => { setError(null); void refreshBlueprints().catch(report); }}>刷新策略库</button></div><label>Blueprint<select value={blueprint} onChange={(event) => { clearAnalysis(); setBlueprint(event.target.value); }}><option value="">选择已完成的训练工件</option>{shownBlueprints.map((item) => <option key={item.blueprint_id} value={item.blueprint_id}>{stringify(item.metadata.game_id ?? item.metadata.rules_version)} · {item.information_sets} 信息集 · {item.blueprint_id.slice(0, 8)}</option>)}</select></label><label className="checkbox"><input type="checkbox" checked={search} onChange={(event) => { clearAnalysis(); setSearch(event.target.checked); }} />启用深度受限、多后续策略搜索</label>{search && <label>搜索深度<select value={depth} onChange={(event) => { clearAnalysis(); setDepth(Number(event.target.value)); }}><option value={1}>1 层</option><option value={2}>2 层</option></select></label>}<p className="small">{session ? `${gameNames[session.game.game_id]} · 状态 v${session.state_version}${session.terminal ? " · 已结束，请创建新手牌" : ""}` : "先在牌局页创建模拟，再为当前节点分析。"}{search ? " 搜索预算 3 秒 / 20 万节点。" : ""}</p><button className="primary full" disabled={!unlocked || !session || session.terminal || uncertain || !blueprint || busy.has("analysis") || busy.has("session")} onClick={() => void analyze()}>{busy.has("analysis") ? "计算中…" : search ? "搜索当前节点" : "查询基础策略"}</button>
            {visibleAnalysis && <div className="strategy-result" aria-live="polite"><span className="badge">{visibleAnalysis.status === "unsupported" ? "当前节点未覆盖" : visibleAnalysis.strategy_source === "blueprint" ? "自博弈基础策略" : "限时近似搜索"}</span>{visibleAnalysis.actions.map((item) => <div className="frequency" key={item.action}><div className="row"><span>{actionName(item.action)}</span><strong>{percent(item.frequency)}</strong></div><meter min={0} max={1} value={item.frequency}>{percent(item.frequency)}</meter></div>)}{visibleAnalysis.warnings.map((warning, index) => <p className="small note" key={index}>{warningText(warning)}</p>)}{visibleAnalysis.actions.length > 0 && <p className="small">频率是策略分布，不是获胜概率；新行动后此结果自动失效。</p>}<details><summary>模型版本与求解诊断</summary><p className="small">{visibleAnalysis.model_version ?? "无匹配工件"}</p><pre>{JSON.stringify(visibleAnalysis.diagnostics, null, 2)}</pre></details></div>}
          </div>
          <details className="research-card adaptation-panel">
            <summary>画像适应实验</summary>
            <p>对选定的两人 Kuhn / Leduc blueprint，评估假设风格下的整局策略调整。此实验使用合成行为模型，与当前手牌或真实玩家画像无关。</p>
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
        </section>}
        {section === "equity" && <section className="panel" aria-labelledby="equity-title"><div className="section-heading"><span className="eyebrow">03 / EQUITY</span><h2 id="equity-title">计算最终底池份额</h2><p>对均匀随机对手范围抽样，平局按共享人数分配。使用标准德州牌组。</p></div><div className="form-grid"><label className="wide">我的底牌<input value={hero} onChange={(event) => { setHero(event.target.value); clearEquity(); }} placeholder="As Kd" spellCheck={false} /></label><label className="wide">公共牌<input value={board} onChange={(event) => { setBoard(event.target.value); clearEquity(); }} placeholder="例如 Qs Jh 2c，翻前留空" spellCheck={false} /></label><label>对手人数<select value={opponents} onChange={(event) => { setOpponents(Number(event.target.value)); clearEquity(); }}>{[1, 2, 3, 4, 5].map((number) => <option key={number} value={number}>{number} 人</option>)}</select></label><label>抽样数<input type="number" min={100} max={10000} value={samples} onChange={(event) => { setSamples(Number(event.target.value)); clearEquity(); }} /></label></div><p className="small">牌点：2–9 / T J Q K A；花色：s ♠ / h ♥ / d ♦ / c ♣。使用种子 {seed}。</p><button className="primary full" disabled={!unlocked || busy.has("equity") || busy.has("strength") || samples < 100 || samples > 10000} onClick={() => void calculateEquity()}>{busy.has("equity") || busy.has("strength") ? "计算中…" : "计算牌力与 equity"}</button>{equity && <div className="equity-result" role="status"><span className="eyebrow">预期底池份额</span><strong>{percent(equity.equity)}</strong><p>抽样标准误 {percent(equity.stderr)} · {equity.samples} 次样本</p><p className="small">{equity.method} · 种子 {equity.seed}</p></div>}{handStrength && <CurrentHandStrength result={handStrength} />}<aside className="note"><p>Equity 不等于行动 EV，不能单独决定下注尺寸。此页不推断对手牌风，也不计算蘑菇奖励。</p></aside></section>}
        {section === "players" && <section className="panel" aria-labelledby="players-title"><div className="section-heading"><span className="eyebrow">04 / OPPONENT MODELS</span><h2 id="players-title">从观察建立画像</h2><p>从当前模拟牌局的公开行动重建统计，固定到当前状态版本；不会读取其他席位的隐藏底牌。</p></div><button className="primary full" disabled={!session || !unlocked || uncertain || busy.has("profiles")} onClick={() => void refreshProfiles()}>{busy.has("profiles") ? "统计中…" : "读取当前模拟画像"}</button>{!session && <p className="small">先在牌局页创建一手模拟。</p>}{profiles && <div className="profile-results"><p className="small">状态 v{profiles.state_version} · {profiles.hand_complete ? "本手完成" : "本手进行中"}</p>{profiles.players.map((player) => <article className="research-card" key={player.player_id}><h3>席位 {player.seat}</h3>{Object.entries(player.statistics).map(([name, stat]) => <div className="profile-stat" key={name}><div className="row"><strong>{name}</strong><span>{stat.successes} / {stat.opportunities} 次机会</span></div><p className="small">后验均值 {percent(stat.posterior_mean)} · 保守区间 {percent(stat.interval[0])}–{percent(stat.interval[1])}</p></div>)}{player.actions.length === 0 && <p className="small">此席位尚无可观察行动。</p>}{player.actions.map((distribution) => <div className="profile-stat" key={distribution.context}><p className="small context-label">{distribution.context} · {distribution.observations} 次观察</p>{Object.entries(distribution.probabilities).map(([action, probability]) => <div className="row small" key={action}><span>{actionName(action)} · {distribution.counts[action] ?? 0} 次</span><strong>{percent(probability)}</strong></div>)}</div>)}</article>)}{profiles.warnings.map((warning, index) => <p className="small note" key={index}>{warning}</p>)}</div>}<div className="research-card"><h3>记录行为与机会</h3><p>VPIP、PFR、3-bet 等指标同时保留分子与真实机会分母，样本稀疏时向先验收缩，并报告不确定性。</p></div><div className="research-card"><h3>区分对手与自己的形象</h3><p>对手画像与自身历史分别建模；对手眼中的形象只使用其当时可见的信息。弃牌未亮牌不会被标成诈唬。</p></div><div className="research-card"><h3>基线独立，调整受约束</h3><p>Pluribus 原方法不根据对手身份适应。画像增强属于项目扩展，需要独立评测；当前工作台仅输出基础策略或搜索结果。</p></div></section>}
        <aside className="research-note"><span className="eyebrow">研究范围</span><p>已实现有限博弈训练与搜索、标准德州模拟。完整六人德州训练与 Pluribus 原系统复现仍未完成。蘑菇收益独立记账，尚未接入策略训练。</p></aside>
      </main>
      <footer>可复现的输入 · 明确的预算 · 可检查的结果</footer>
    </div>
  );
}
