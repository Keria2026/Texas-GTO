import type { Recommendation } from './recommendation.ts';
import type { CaptureState } from './capture-state.ts';
import { PROFILE_ACTIONS } from './capture-profiles.ts';
import type { CaptureProfiles } from './capture-profiles.ts';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { RecordEnvelope, BusinessRecord, Session } from './model.ts';
import { BACKENDS } from './transport.ts';
import './sidepanel.css';

interface Status {
  serverCapture?: CaptureState;
  recommendation?: Recommendation;
  serverProfiles?: CaptureProfiles;
  sessions: Session[]; tabs: { id: number; label: string }[]; transportState: string;
  transportError?: string; backendURL: string; fault?: string; wasm: string; recent: RecordEnvelope[];
  business?: Session; businessStatus: string; businessPending: string; backendCapabilities: string[]; businessRecent: BusinessRecord[];
}
async function command(message: object): Promise<unknown> {
  const response = await chrome.runtime.sendMessage(message) as { ok: boolean; value: unknown; error?: string };
  if (!response?.ok) throw Error(response?.error ?? '扩展无响应，请重新打开侧栏');
  return response.value;
}
const states: Record<string, string> = { ready: '已连接', disconnected: '未连接', connecting: '连接中', retrying: '等待重连', unsupported: '后端不支持', error: '通信错误' };
function App() {
  const [status, setStatus] = useState<Status>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState('');
  const [raw, setRaw] = useState(false);
  const [token, setToken] = useState('');
  const [url, setURL] = useState(BACKENDS[0]);
  const local = url.startsWith('ws://127.0.0.1:');
  useEffect(() => { if (status?.backendURL) setURL(status.backendURL); }, [status?.backendURL]);
  const refreshRequest = useRef(0);
  const refresh = useCallback(async () => {
    const current = ++refreshRequest.current;
    try {
      const result = await command({ type: 'status' }) as Status;
      if (current === refreshRequest.current) setStatus(result);
    } catch {
      if (current === refreshRequest.current) {
        setStatus(undefined); setError('无法读取扩展状态，请检查扩展错误后重新打开侧栏。');
      }
    }
  }, []);
  useEffect(() => {
    void refresh(); const timer = setInterval(() => { void refresh(); }, 1500);
    const changed = (message: any, sender: chrome.runtime.MessageSender) => {
      if (sender.id === chrome.runtime.id && message?.type === 'state_changed') {
        setStatus(previous => previous ? { ...previous, recommendation: undefined } : previous); void refresh();
      }
    };
    chrome.runtime.onMessage.addListener(changed);
    return () => { refreshRequest.current++; clearInterval(timer); chrome.runtime.onMessage.removeListener(changed); };
  }, [refresh]);
  async function run(message: object) {
    setBusy(true); setError('');
    try { await command(message); if ('type' in message && message.type === 'connect') setToken(''); await refresh(); }
    catch (e) { setError(e instanceof Error ? e.message : '操作失败'); }
    finally { setBusy(false); }
  }
  async function download(source: string, withRaw: boolean) {
    if (withRaw && !confirm('原始载荷可能含认证或账号信息，仅供本地诊断。确认导出？')) return;
    setBusy(true); setError('');
    try {
      const data = await command({ type: 'export', source, raw: withRaw });
      const href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
      const link = document.createElement('a'); link.href = href; link.download = `tgto-${source}${withRaw ? '-private' : ''}.json`; link.click();
      setTimeout(() => URL.revokeObjectURL(href), 1000);
    } catch { setError('导出失败，原记录仍保存在本地。'); } finally { setBusy(false); }
  }
  const active = status?.sessions.find(s => s.state === 'active');
  const latest = status?.sessions[0];
  const pending = status?.sessions.reduce((n, s) => n + BigInt(s.last) - BigInt(s.ack), 0n) ?? 0n;
  return <main>
    <header><h1>TGTO 采集</h1><span className="version">0.6.1 预览</span></header>
    <p className="intro">逐条记录网络消息，离线保留，确认后完成传输。</p>
    {(error || status?.fault) && <p className="notice" role="alert">{error || status?.fault}</p>}
    <section aria-labelledby="recommendation-title"><h2 id="recommendation-title">八人局行动建议</h2>
      <RecommendationView recommendation={status?.recommendation} connected={status?.transportState === 'ready'} />
    </section>
    {!status ? <p role="status">正在读取本地采集状态…</p> : <>
      <section aria-labelledby="capture-title"><h2 id="capture-title">页面采集</h2>
        <p role="status">{active ? '正在采集' : latest?.state === 'interrupted' ? '采集已中断，存在覆盖缺口' : '采集未开始或已停止'}</p>
        <label htmlFor="tab">目标标签页</label>
        <select id="tab" value={tab || String(status.tabs[0]?.id ?? '')} disabled={!!active || busy} onChange={e => setTab(e.target.value)}>
          {!status.tabs.length && <option value="">请先打开 WPK 网页</option>}
          {status.tabs.map(t => <option key={t.id} value={t.id}>{t.label}</option>)}
        </select>
        <label className="check"><input type="checkbox" checked={raw} disabled={!!active || busy} onChange={e => setRaw(e.target.checked)} />额外保存原始载荷，仅限本地诊断</label>
        <p className="help">默认保存时间、方向、长度与摘要。原始载荷可能含敏感信息，不会上传后端。</p>
        <div className="actions"><button disabled={busy || !!active || !status.tabs.length} onClick={() => void run({ type: 'start', tabId: Number(tab || status.tabs[0]?.id), raw })}>开始采集</button>
          <button className="secondary" disabled={busy || !active} onClick={() => void run({ type: 'stop' })}>停止采集</button></div>
        <p className="help">开始之前的消息无法补取。后台重启会恢复此前的采集；中断当手不推荐行动。刷新页面需重新开始采集。</p>
      </section>
      <section aria-labelledby="business-title"><h2 id="business-title">牌桌业务数据</h2>
        <p role="status">{({ ready: '业务适配已就绪', waiting_client: '等待 WPK 客户端加载', waiting_seat: '等待本人入座', interrupted: '业务采集已中断', stopped: '业务采集已停止', inactive: '业务采集未启动' } as Record<string, string>)[status.businessStatus] ?? status.businessStatus}</p>
        {status.business?.error && <p className="notice">{status.business.error}。已记录数据可导出；重新开始采集会创建新会话。</p>}
        {status.business?.checkpoint && <BusinessView checkpoint={status.business.checkpoint} stale={status.business.state !== 'active'} />}
        {status.business && <>
          <p className="help">业务写入 {status.business.last} 条，后端确认 {status.business.ack} 条。{!status.backendCapabilities.includes('capture_events_v1') && '后端尚未声明业务事件能力，记录保留在本地。'}</p>
          <p className="help">全部业务会话共 {status.businessPending} 条待确认记录。</p>
          <button className="secondary" disabled={busy} onClick={() => void download(status.business!.id, false)}>导出业务事件</button>
          <ol className="events">{status.businessRecent.slice(0, 5).map(r => <li key={r.sequence}><span className="number">#{r.sequence}</span>{r.kind}<small>{r.hand_id ?? r.data.reason ?? r.captured_at}</small></li>)}</ol>
        </>}
        <p className="help">读取本账号可见数据。加注范围保留原始报告口径；规则和策略覆盖仍为未验证。</p>
      </section>
      <section aria-labelledby="backend-title"><h2 id="backend-title">研究服务</h2>
        <p role="status">{states[status.transportState] ?? status.transportState}</p>
        {status.transportError && <p className="notice">{status.transportError}</p>}
        {local ? <p className="help">自动连接本地算法端 {new URL(url).host}，无需访问令牌。</p> : <>
          <label htmlFor="backend">服务地址</label><select id="backend" value={url} onChange={e => setURL(e.target.value)}>
            {BACKENDS.map(b => <option key={b}>{b}</option>)}</select>
          <label htmlFor="token">远程服务访问令牌</label><input id="token" type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} />
        </>}
        {!local && <p className="help">远程服务使用独立访问令牌。</p>}
        <div className="actions"><button disabled={busy || (!local && !token.trim())} onClick={() => void run({ type: 'connect', url, token: local ? '' : token.trim() })}>重新连接</button>
          <button className="secondary" disabled={busy || status.transportState === 'disconnected'} onClick={() => void run({ type: 'disconnect' })}>断开服务</button></div>
      </section>
      <section aria-labelledby="analysis-title"><h2 id="analysis-title">服务器牌局与算法状态</h2>
        {status.serverCapture ? <>
          <p>服务器已确认并重建至 #{status.serverCapture.through_sequence}。</p>
          <BusinessView checkpoint={{ ...status.serverCapture.state,
            hand_number: String(status.serverCapture.state.hand_id ?? '').split(':')[1] }} stale={false} />
          <p className="help">当前建议与此版本绑定；继续行动后会重新校验。</p>
        </> : <p className="help">{status.transportState !== 'ready' ? '服务器未连接。' : !status.backendCapabilities.includes('capture_state_v1')
          ? '当前后端未提供牌局重建状态。' : '等待服务器确认并恢复当前业务状态。'} 暂无可用行动建议。</p>}
      </section>
      <section aria-labelledby="profiles-title"><h2 id="profiles-title">本次采集的玩家画像</h2>
        <p className="help">按本次采集的已观察行动统计。不会跨采集合并玩家；以下频率不是行动建议或胜率。</p>
        {status.serverProfiles ? <ProfilesView profiles={status.serverProfiles} />
          : <p role="status" className="help">{status.transportState !== 'ready' ? '服务未连接，已清除旧画像。'
            : !status.backendCapabilities.includes('capture_profiles_v1') ? '当前服务尚未提供玩家画像。'
              : '等待服务器确认本次采集的画像。'}</p>}
      </section>
      <section aria-labelledby="queue-title"><h2 id="queue-title">网络记录</h2>
        {latest ? <><dl><dt>WASM 核心</dt><dd>已就绪</dd><dt>累计写入</dt><dd>{latest.last}</dd><dt>后端已确认</dt><dd>{latest.ack}</dd><dt>待确认</dt><dd>{String(BigInt(latest.last) - BigInt(latest.ack))}</dd><dt>待确认大小</dt><dd>{(latest.pendingBytes / 1024).toFixed(1)} KiB</dd></dl>
          <p className="help">全部网络会话共 {String(pending)} 条待确认记录。</p>
          <div className="actions"><button className="secondary" disabled={busy} onClick={() => void download(latest.id, false)}>导出元数据</button>
            {latest.raw && <button className="secondary" disabled={busy} onClick={() => void download(latest.id, true)}>导出本地载荷</button>}</div>
          <h3>最近消息</h3><ol className="events">{status.recent.map(r => <li key={r.sequence}><div><span className="number">#{r.sequence}</span> {r.type === 'network_message' ? `${r.data.direction === 'received' ? '接收' : '发送'} · ${r.data.byte_length} B` : r.type}</div><small>{r.data.reason ?? r.data.endpoint ?? r.captured_at}</small></li>)}</ol>
        </> : <p className="help">开始采集后，逐条记录会先写入本地。尚无记录。</p>}
      </section>
    </>}
    <footer>网络记录与业务事件独立确认。缺少结算配对或积分未对齐时保持待确认。</footer>
  </main>;
}
const actionNames = { FOLD: '弃牌', CHECK: '过牌', CALL: '跟注', BET: '下注', RAISE: '加注', ALL_IN: '全下' };
const unavailableReasons: Record<string, string> = {
  not_hero_turn: '等待轮到你行动。', requires_eight_participants: '当前推荐支持八人实际参与的牌局。',
  incomplete_action_history: '本手采集有缺口，等待下一手完整数据。', hero_cards_unavailable: '等待确认你的两张手牌。',
  mushroom_ledger_unavailable: '蘑菇池及缴费归属尚未确认，继续采集结算记录。',
  unmapped_mushroom_owner: '蘑菇池存在尚未对应到座位的缴费权益。',
  observation_expired: '牌局信息已过期，等待新的牌桌状态。', public_state_incomplete: '等待完整的行动、筹码和牌桌快照。',
  strategy_not_trained: '当前节点尚无已训练策略，暂不推荐行动。', model_unavailable: '训练模型尚未就绪。',
  client_legal_actions_conflict: '牌桌可选行动与规则校验不一致，建议已停止。',
  query_timeout: '本次查询超时，建议已清除。', model_or_state_invalid: '模型或牌局校验失败，暂不推荐行动。',
};
function RecommendationView({ recommendation, connected }: { recommendation?: Recommendation; connected: boolean }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 200); return () => clearInterval(timer); }, []);
  if (!connected) return <p role="status">本地服务未连接，建议已清除。</p>;
  if (!recommendation) return <p role="status">等待当前牌局的有效建议。</p>;
  if (recommendation.status !== 'ready') return <p role="status">{unavailableReasons[recommendation.reason] ?? '当前牌局尚未通过完整校验，暂不推荐行动。'}</p>;
  if (now >= recommendation.expires_at_ms) return <p role="status">建议已过期，等待新的牌桌状态。</p>;
  const chosen = recommendation.actions[recommendation.selected_action!];
  const amount = (a: typeof chosen) => a.raise_to !== null ? `至 ${a.raise_to} 分，本次投入 ${a.amount_delta} 分`
    : a.amount_delta ? `${a.amount_delta} 分` : '';
  return <>
    <p className="recommended-action" role="status"><strong>{actionNames[chosen!.action]} {amount(chosen!)}</strong></p>
    <p className="help">第 {recommendation.hand_id!.split(':')[1]} 手 · 按策略频率抽取一次建议</p>
    <ul className="events">{recommendation.actions.map(a => <li key={a.label}>{actionNames[a.action]} {amount(a)}
      <small>策略频率 {(a.probability * 100).toFixed(1)}%</small></li>)}</ul>
    <p className="notice">试运行策略：仅计算两手视野，尚未完成实局质量验收；玩家画像尚未用于调整建议。频率不是胜率。</p>
    <details><summary>模型来源</summary><p className="help">Pluribus 自博弈策略 · 累计 {recommendation.model!.iterations} 轮 · {recommendation.model!.version.slice(0, 12)}</p></details>
  </>;
}

const streets = { preflop: '翻前', flop: '翻牌', turn: '转牌', river: '河牌', unknown: '轮次未知' };
const qualityNames = { complete_observed_history: '观察历史完整', incomplete_history: '历史不完整', unknown_street: '轮次待确认' };
function ProfilesView({ profiles }: { profiles: CaptureProfiles }) {
  return <>
    {!profiles.players.length ? <p className="help">尚无可展示的玩家样本。</p> : <ul className="events">
      {profiles.players.map(player => <li key={player.player_id}>
        <strong>{player.player_id === 'hero' ? '本人' : `玩家 ${player.player_id.slice(7)}`}</strong>
        <p className="help">已观察 {player.action_observations.toLocaleString('zh-CN')} 次行动</p>
        {!player.contexts.length ? <p className="help">尚无有效行动样本，不计算频率。</p>
          : player.contexts.map(c => <details key={`${c.street}:${c.quality}`}>
            <summary>{streets[c.street]} · {c.observations.toLocaleString('zh-CN')} 次 · {qualityNames[c.quality]}</summary>
            <dl>{PROFILE_ACTIONS.map(action => <React.Fragment key={action}>
              <dt>{actionNames[action]}</dt><dd>{c.counts[action].toLocaleString('zh-CN')} 次 · {(c.frequencies[action] * 100).toFixed(1)}%</dd>
            </React.Fragment>)}</dl>
            <p className="help">只代表已观察到的行动比例，不能推断所有情形下的选择概率。</p>
          </details>)}
      </li>)}
    </ul>}
    {!!profiles.warnings.length && <p className="help">样本提示：{profiles.warnings.join('；')}</p>}
  </>;
}
function BusinessView({ checkpoint, stale }: { checkpoint: Record<string, unknown>; stale: boolean }) {
  const s = checkpoint as { table_id?: string; hand_number?: number; street?: string; closed?: boolean; hero_cards?: (string | null)[]; board?: string[];
    actions?: { player_id: string; action: string; amount_delta: number }[];
    settlements?: { hand_number: number; status: string; reason: string | null; players: { player_id: string; net_chips: number | null }[] }[] };
  const result = s.settlements?.at(-1);
  return <>
    {stale && <p className="help">以下为中断前最后记录，不能作为当前牌桌状态。</p>}
    {s.closed && <p className="help">已记录房间关闭；这不表示账户最终对账完成。</p>}
    <dl><dt>手数 / 阶段</dt><dd>{s.hand_number ?? '未知'} / {s.street ?? '未知'}</dd><dt>本人手牌</dt><dd>{s.hero_cards?.map(c => c ?? '未知').join(' ') || '未知'}</dd><dt>公共牌</dt><dd>{s.board?.join(' ') || '尚无'}</dd></dl>
    {!!s.actions?.length && <><h3>本手最近行动</h3><ol className="events">{s.actions.slice(-5).map((a, i) => <li key={i}>{a.player_id} · {a.action} · {a.amount_delta}</li>)}</ol></>}
    {result && <><h3>最近结算 · 第 {result.hand_number} 手</h3><p>{result.status === 'reconciled' ? '已配对，积分校验通过' : `待确认：${result.reason ?? '金额缺失'}`}</p><dl>{result.players.map(p => <React.Fragment key={p.player_id}><dt>{p.player_id}</dt><dd>{p.net_chips ?? '未知'}</dd></React.Fragment>)}</dl></>}
  </>;
}
createRoot(document.getElementById('root')!).render(<App />);
