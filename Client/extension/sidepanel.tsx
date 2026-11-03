import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { Recommendation } from './recommendation.ts';
import type { Session } from './model.ts';
import './sidepanel.css';

interface Status {
  sessions: Session[]; transportState: string; fault?: string;
  recommendation?: Recommendation; business?: Session; businessStatus: string;
}
async function command(message: object): Promise<unknown> {
  const response = await chrome.runtime.sendMessage(message) as { ok: boolean; value: unknown; error?: string };
  if (!response?.ok) throw Error(response?.error ?? '扩展无响应，请重新打开侧栏');
  return response.value;
}
const connectionNames: Record<string, string> = { ready: '已连接', disconnected: '未连接', connecting: '连接中', retrying: '正在重连', unsupported: '服务不兼容', error: '连接异常' };
const businessNames: Record<string, string> = { ready: '正在同步牌局', waiting_client: '等待 WPK 页面加载', waiting_seat: '等待你入座', waiting_hand: '等待下一手完整开局', interrupted: '采集已中断，请重新开始', stopped: '对局已结束', inactive: '打开 WPK 牌桌后开始' };
const actionNames: Record<string, string> = { FOLD: '弃牌', CHECK: '过牌', CALL: '跟注', BET: '下注', RAISE: '加注', ALL_IN: '全下', ANTE: '前注', SMALL_BLIND: '小盲', BIG_BLIND: '大盲', STRADDLE: 'Straddle' };
const streets: Record<string, string> = { preflop: '翻前', flop: '翻牌', turn: '转牌', river: '河牌', showdown: '摊牌', between_hands: '等待发牌', unknown: '等待牌局' };
const unavailableReasons: Record<string, string> = {
  ante_not_supported: '已采到本桌前注，当前策略模型尚未支持这一规则，暂不推荐。',
  not_hero_turn: '等待轮到你行动。', requires_eight_participants: '当前推荐支持八人实际参与的牌局。',
  incomplete_action_history: '本手采集有缺口，等待下一手完整数据。', hero_cards_unavailable: '等待确认你的两张手牌。',
  mushroom_ledger_unavailable: '蘑菇池及缴费归属尚未确认，继续采集结算记录。',
  unmapped_mushroom_owner: '蘑菇池存在尚未对应到座位的缴费权益。',
  observation_expired: '牌局信息已过期，等待新的牌桌状态。', public_state_incomplete: '等待完整的行动、筹码和牌桌快照。',
  strategy_not_trained: '当前节点尚无已训练策略，暂不推荐行动。', model_unavailable: '训练模型尚未就绪。',
  strategy_feedback_unavailable: '当前局面的训练反馈不足，暂不推荐行动。',
  client_legal_actions_conflict: '牌桌可选行动与规则校验不一致，建议已停止。',
  query_timeout: '本次查询超时，建议已清除。', model_or_state_invalid: '模型或牌局校验失败，暂不推荐行动。',
};

function App() {
  const [status, setStatus] = useState<Status>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const request = useRef(0);
  const refresh = useCallback(async () => {
    const id = ++request.current;
    try {
      const next = await command({ type: 'status' }) as Status;
      if (id === request.current) setStatus(next);
    } catch {
      if (id === request.current) { setStatus(undefined); setError('无法读取扩展状态，请重新打开侧栏。'); }
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 1500);
    const changed = (message: { type?: string }, sender: chrome.runtime.MessageSender) => {
      if (sender.id === chrome.runtime.id && message?.type === 'state_changed') {
        setStatus(old => old ? { ...old, recommendation: undefined } : old); void refresh();
      }
    };
    chrome.runtime.onMessage.addListener(changed);
    return () => { request.current++; clearInterval(timer); chrome.runtime.onMessage.removeListener(changed); };
  }, [refresh]);
  const active = status?.sessions.some(s => s.state === 'active') ?? false;
  const connected = status?.transportState === 'ready';
  async function start() {
    setBusy(true); setError('');
    try { await command({ type: 'start_game' }); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : '开始失败，请重试'); }
    finally { setBusy(false); }
  }
  return <main>
    <header><h1>TGTO</h1><span className={connected ? 'connection online' : 'connection'} role="status"><i aria-hidden="true" />{connectionNames[status?.transportState ?? 'disconnected']}</span></header>
    <button className="start" disabled={busy || active || !status} onClick={() => void start()}>{busy ? '正在开始…' : active ? '对局进行中' : '开始对局'}</button>
    <p className="caption" role="status">{!status ? '正在读取状态…' : businessNames[status.businessStatus] ?? '正在获取牌局'}</p>
    {(error || status?.fault) && <p className="notice" role="alert">{error || status?.fault}</p>}
    <section className="advice" aria-labelledby="recommendation-title"><h2 id="recommendation-title">推荐行动</h2>
      <RecommendationView recommendation={active ? status?.recommendation : undefined} connected={connected} />
    </section>
    <section aria-labelledby="table-title"><h2 id="table-title">当前牌局</h2>
      {status?.business?.checkpoint ? <BusinessView checkpoint={status.business.checkpoint}
        stale={!active || status.businessStatus !== 'ready'} /> : <p className="empty">开始对局后，这里会显示手牌、公共牌和玩家筹码。</p>}
    </section>
  </main>;
}
function RecommendationView({ recommendation: r, connected }: { recommendation?: Recommendation; connected: boolean }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 200); return () => clearInterval(timer); }, []);
  if (!connected) return <p className="empty" role="status">等待连接本地服务。开始后会自动重连。</p>;
  if (!r) return <p className="empty" role="status">等待当前牌局的有效建议。</p>;
  if (r.status !== 'ready') return <p className="empty" role="status">{unavailableReasons[r.reason] ?? '当前牌局信息不足，暂不推荐行动。'}</p>;
  if (Math.max(now, Date.now()) >= r.expires_at_ms) return <p className="empty" role="status">建议已过期，等待新的牌桌状态。</p>;
  const chosen = r.actions[r.selected_action!];
  return <div role="status"><strong className="action">{actionNames[chosen.action]}</strong>
    <p className="amount">{chosen.raise_to !== null ? `至 ${chosen.raise_to} 分` : chosen.amount_delta ? `${chosen.amount_delta} 分` : '无需投入筹码'}</p>
    {chosen.raise_to !== null && <p className="caption">本次投入 {chosen.amount_delta} 分</p>}
  </div>;
}
function Cards({ cards }: { cards: (string | null)[] }) {
  const suits: Record<string, string> = { s: '♠', h: '♥', d: '♦', c: '♣' };
  return <div className="cards">{cards.map((card, i) => <span key={i} className={`card ${card && /[hd]$/.test(card) ? 'red' : ''}`}>
    {card ? <>{card.slice(0, -1).replace('T', '10')}<span>{suits[card.slice(-1)] ?? ''}</span></> : '—'}
  </span>)}</div>;
}
function BusinessView({ checkpoint, stale }: { checkpoint: Record<string, unknown>; stale: boolean }) {
  const s = checkpoint as { hand_number?: number; street?: string; hero_cards?: (string | null)[]; board?: string[];
    players?: { player_id: string; seat: number; stack: number | null; folded?: boolean }[];
    turn?: { player_id: string } | null; actions?: { player_id: string; action: string; amount_delta: number }[] };
  const name = (id: string) => id === 'hero' ? '你' : `${s.players?.find(p => p.player_id === id)?.seat ?? '?'} 号位`;
  return <div className={stale ? 'table stale' : 'table'}>
    {stale && <p className="notice">最后收到的牌局信息，等待更新。</p>}
    <p className="hand-line">第 {s.hand_number ?? '—'} 手 <span>{streets[s.street ?? 'unknown'] ?? s.street}</span></p>
    <div className="card-row"><div><h3>你的手牌</h3><Cards cards={s.hero_cards ?? [null, null]} /></div></div>
    <h3>公共牌</h3><Cards cards={[...(s.board ?? []), ...Array(Math.max(0, 5 - (s.board?.length ?? 0))).fill(null)]} />
    <p className="turn">{s.turn ? `当前行动：${name(s.turn.player_id)}` : '等待下一次行动'}</p>
    {!!s.players?.length && <table><thead><tr><th>玩家</th><th>筹码 / 分</th></tr></thead><tbody>
      {s.players.map(p => <tr key={p.player_id} className={s.turn?.player_id === p.player_id ? 'acting' : ''}><td>{name(p.player_id)}{p.folded ? ' · 已弃牌' : ''}</td><td>{p.stack?.toLocaleString('zh-CN') ?? '未知'}</td></tr>)}
    </tbody></table>}
    {!!s.actions?.length && <><h3>最近行动</h3><ol className="history">{s.actions.slice(-5).map((a, i) => <li key={i}><span>{name(a.player_id)}</span><span>{actionNames[a.action] ?? a.action}{a.amount_delta > 0 ? ` ${a.amount_delta} 分` : ''}</span></li>)}</ol></>}
  </div>;
}
createRoot(document.getElementById('root')!).render(<App />);
