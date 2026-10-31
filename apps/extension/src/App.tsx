import { useEffect, useRef, useState } from "react";
import { parseHealthResponse } from "@texas-gto/protocol";
import type { HealthResponse } from "@texas-gto/protocol";

const sections = {
  hand: { label: "牌局", title: "从一手牌开始", text: "牌谱导入与行动回放正在建设。接入牌局后，这里将展示位置、公共牌与行动历史。" },
  strategy: { label: "策略", title: "策略引擎尚在构建", text: "核心采用 Pluribus 的自博弈与实时搜索方法。完成训练和独立验证后，才会提供行动频率与收益分析。" },
  equity: { label: "牌力", title: "等待有效牌局", text: "牌型、范围胜率与坚果牌分析需要完整且通过校验的输入。当前版本尚未提供计算功能。" },
  players: { label: "玩家", title: "积累可解释的观察", text: "后续将按位置、街道和真实行动机会记录玩家倾向。当前没有采集或保存玩家数据。" },
} as const;
type Section = keyof typeof sections;
type Connection = "idle" | "checking" | "connected" | "error";

export function App() {
  const [section, setSection] = useState<Section>("hand");
  const [connection, setConnection] = useState<Connection>("idle");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  useEffect(() => () => activeRequest.current?.abort(), []);

  async function checkConnection() {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setHealth(null);
    setConnection("checking");
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch("http://127.0.0.1:8765/v1/health", {
        signal: controller.signal,
        cache: "no-store",
        credentials: "omit",
      });
      if (!response.ok) throw new Error("Health request failed");
      const result = parseHealthResponse(await response.json());
      if (activeRequest.current !== controller || controller.signal.aborted) return;
      setHealth(result);
      setConnection("connected");
    } catch {
      if (activeRequest.current === controller) setConnection("error");
    } finally {
      window.clearTimeout(timeout);
    }
  }

  const current = sections[section];
  const status = { idle: "尚未连接", checking: "正在检查", connected: "服务可用", error: "连接未成功" }[connection];

  return (
    <div className="workspace">
      <header className="app-header">
        <div className="brand"><span className="brand-mark" aria-hidden="true">♠</span><span>Texas GTO</span></div>
        <span className="version">离线研究 · 0.1</span>
      </header>
      <main>
        <section className="intro" aria-labelledby="intro-title">
          <span className="eyebrow">PLURIBUS RESEARCH</span>
          <h1 id="intro-title">让每个决策，<br />都有依据。</h1>
          <p>从可验证的牌局出发，逐步建立多人策略与复盘能力。</p>
        </section>
        <section className="connection" aria-label="分析服务">
          <div><h2>分析服务</h2><p role="status" className={connection === "error" ? "error" : ""}>{status}{health ? ` · v${health.version}` : ""}</p></div>
          <button className="primary" onClick={() => void checkConnection()} disabled={connection === "checking"}>
            {connection === "checking" ? "检查中…" : "检查连接"}
          </button>
        </section>
        {connection === "error" && <p className="connection-help">请确认本地服务已启动，并允许当前界面连接。</p>}
        <nav className="tabs" aria-label="分析视图">
          {(Object.keys(sections) as Section[]).map((key) => (
            <button key={key} aria-pressed={key === section} onClick={() => setSection(key)}>{sections[key].label}</button>
          ))}
        </nav>
        <section className="empty-state" aria-labelledby="section-title">
          <span className="card-symbol" aria-hidden="true">◇</span>
          <h2 id="section-title">{current.title}</h2>
          <p>{current.text}</p>
        </section>
        <aside className="research-note">
          <span className="eyebrow">当前版本</span>
          <h2>先验证，再推荐</h2>
          <p>自博弈策略与搜索尚未完成。当前版本不生成下注建议。</p>
        </aside>
      </main>
      <footer>自博弈基础策略 → 实时搜索 → 画像增强</footer>
    </div>
  );
}
