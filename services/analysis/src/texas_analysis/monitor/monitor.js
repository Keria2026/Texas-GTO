const el = id => document.getElementById(id);
const number = n => n.toLocaleString('zh-CN');
const streets = ['翻前', '翻牌', '转牌', '河牌'];
const names = { FOLD: '弃牌', CHECK: '过牌', CALL: '跟注', BET: '下注', RAISE: '加注', ALL_IN: '全下' };
const reasons = { strategy_not_trained: '信息集尚无平均策略', strategy_feedback_unavailable: '有策略记录，但收益反馈不足', covered_blueprint: '命中自博弈基线', searched_strategy: '搜索已给出行动' };
const phases = { training: '正在训练', saving: '正在保存', paused: '为前台查询暂停', starting: '正在恢复模型', waiting_for_idle: '等待空闲', stopped: '已停止', limited: '达到资源限制', error: '训练异常', disabled: '未启用' };
async function api(path, body) {
  const response = await fetch(path, { method: body ? 'POST' : 'GET', headers: { 'X-Texas-Local': '1', ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : `请求失败 (${response.status})`);
  return data;
}
function text(id, value) { el(id).textContent = value; }
function row(parent, values) { const tr = document.createElement('tr'); for (const v of values) { const td = document.createElement('td'); td.textContent = v; tr.append(td); } parent.append(tr); }
async function refresh() {
  try {
    const { worker, model: m } = await api('/v1/training-monitor');
    text('connection', '本地服务已连接'); text('phase', worker.enabled ? phases[worker.phase] ?? worker.phase : '训练已关闭');
    el('error').hidden = !worker.reason; text('error', worker.reason ?? '');
    if (!m.available) { text('freshness', m.reason); return; }
    text('freshness', `当前规则：每人 4 分前注 · 统计于 ${new Date(m.sampled_at * 1000).toLocaleTimeString()}，每分钟更新`);
    text('rounds', `${number(m.rule_iterations)} 轮`); text('sets', number(m.information_sets)); text('averaged', number(m.averaged_information_sets));
    text('ratio', m.information_sets ? `${(100 * m.averaged_information_sets / m.information_sets).toFixed(2)}%` : '—');
    el('stages').replaceChildren(); for (let i = 0; i < 8; i++) { const r = m.stages.find(s => s.stage === i); row(el('stages'), [`第 ${Math.floor(i / 4) + 1} 手 · ${streets[i % 4]}`, number(r?.total ?? 0), number(r?.averaged ?? 0)]); }
    text('feedback', `第一手同时有平均策略和非零遗憾反馈：${number(m.first_hand_average_with_regret)} 个。仍不代表这些节点已充分训练。`);
    text('schedule', `第一手翻前按采样累计；其余阶段每 ${m.snapshot_interval} 轮保存一次平均策略快照。下一次：累计第 ${number(m.next_snapshot)} 轮。`);
    text('roots', `${number(m.training_roots)} / ${number(m.observed_openings)}`); text('size', `${(m.bytes / 1024 ** 3).toFixed(2)} GiB`); text('iterations', `${number(m.iterations)} 轮（包含规则变更前）`);
  } catch (e) { text('connection', '服务暂不可用'); el('error').hidden = false; text('error', `无法更新训练状态：${e.message}。稍后自动重连。`); }
  finally { setTimeout(refresh, 5000); }
}
let runs = [];
el('simulate').addEventListener('submit', async event => {
  event.preventDefault(); el('run').disabled = true; text('run', '正在测试…'); text('test-status', '生成合法牌局并查询已保存的策略…'); el('result').hidden = true;
  const seed = el('seed').value === '' ? crypto.getRandomValues(new Uint32Array(1))[0] : Number(el('seed').value);
  try {
    const r = await api('/v1/training-monitor/simulate', { seed, street: Number(el('street').value), search: el('search').checked });
    el('result').hidden = false; text('verdict', r.status === 'ready' ? '可以给出行动' : '本局暂不能推荐'); text('timing', `${r.elapsed_ms} ms`);
    text('case', `种子 ${r.seed} · ${streets[r.street]} · 你在 ${r.hero_seat} 号位 · 模型第 ${number(r.iterations)} 轮`);
    text('cards', `手牌 ${r.hero_cards.join(' ')}　公共牌 ${r.board.join(' ') || '尚未发出'}`);
    text('finances', `底池 ${r.pot} 分 · 蘑菇池 ${r.mushroom_pool} 分 · 需跟注 ${r.to_call} 分`);
    const chosen = r.actions[r.selected_action]; text('selection', chosen ? `${names[chosen.action]}${chosen.raise_to !== null ? `至 ${chosen.raise_to} 分` : chosen.amount_delta ? ` ${chosen.amount_delta} 分` : ''}` : reasons[r.reason] ?? r.reason);
    el('actions').replaceChildren(); for (const a of r.actions) row(el('actions'), [`${names[a.action]}${a.raise_to !== null ? `至 ${a.raise_to}` : ''}`, `${a.amount_delta} 分`, `${(a.probability * 100).toFixed(1)}%`]);
    const search = { complete: '搜索完成', coverage_missing: '搜索后续节点缺少策略', budget: '搜索预算用尽', profile_rejected: '画像候选未通过' };
    text('search-result', `基线平均策略：${r.blueprint_hit ? '命中' : '未命中'}。${r.search ? search[r.search.status] ?? r.search.status : '未启用搜索'}。策略频率不是胜率。`);
    text('stacks', r.stacks.map((s, i) => `${i + 1} 号位 ${s} 分`).join(' · ')); el('history').replaceChildren();
    for (const h of r.history) { const li = document.createElement('li'); li.textContent = `${h.seat} 号位 · ${h.street} · ${h.action} · 投入 ${h.amount} 分`; el('history').append(li); }
    runs = [r, ...runs].slice(0, 20); el('runs').replaceChildren();
    for (const item of runs) { const li = document.createElement('li'); li.textContent = `种子 ${item.seed} · 第 ${item.iterations} 轮 · ${reasons[item.reason] ?? item.reason}`; el('runs').append(li); }
    text('test-summary', `最近 ${runs.length} 次：${runs.filter(x => x.status === 'ready').length} 次给出行动，${runs.filter(x => x.blueprint_hit).length} 次命中平均策略。仅代表这些模拟样本。`);
    text('test-status', '测试完成；未写入训练数据。');
  } catch (e) { text('test-status', `测试未完成：${e.message}`); }
  finally { el('run').disabled = false; text('run', '生成牌局并测试'); }
});
void refresh();
