import test from 'node:test';
import assert from 'node:assert/strict';
import WS, { WebSocketServer } from 'ws';
import { database, until } from './helpers.ts';
import { currentRecommendation, parseRecommendation } from '../extension/recommendation.ts';
import { ResearchSocket } from '../extension/transport.ts';
import type { CaptureState } from '../extension/capture-state.ts';

function ready(source = 'synthetic-eight', through = '1'): any {
  return {
    schema: 'tgto.capture-recommendation/0.1', type: 'capture.recommendation', source_id: source,
    through_sequence: through, hand_id: 'table-1:2', status: 'ready', reason: 'covered_blueprint',
    actions: [{ action: 'FOLD', label: 'fold', amount_delta: 0, raise_to: null, probability: 0.25 },
      { action: 'CALL', label: 'call', amount_delta: 8, raise_to: null, probability: 0.25 },
      { action: 'RAISE', label: 'raise_min', amount_delta: 16, raise_to: 16, probability: 0.5 }],
    selected_action: 2, issued_at_ms: Date.now(), expires_at_ms: Date.now() + 9000, elapsed_ms: 2,
    model: { source: 'pluribus_blueprint', version: 'a'.repeat(64), iterations: 50,
      rules_version: 'mushroom-context-v1:synthetic-test', quality: 'finite_horizon_unvalidated',
      horizon_hands: 2, opponent_adaptation: false }, warnings: ['two_hand_horizon'],
  };
}
test('recommendation rejects malformed amounts, frequencies, provenance and unavailable actions', () => {
  assert.equal(parseRecommendation(ready()).actions.length, 3);
  for (const change of [
    (m: any) => { m.actions[2].raise_to = 8; },
    (m: any) => { m.actions[1].amount_delta = -1; },
    (m: any) => { m.actions[0].probability = NaN; },
    (m: any) => { m.actions[0].probability = 1; },
    (m: any) => { m.actions[1].raise_to = 8; },
    (m: any) => { m.selected_action = 3; },
    (m: any) => { m.model.source = 'heuristic'; },
    (m: any) => { m.model.iterations = 0; },
    (m: any) => { m.model.opponent_adaptation = true; },
    (m: any) => { m.status = 'unavailable'; },
    (m: any) => { m.expires_at_ms += 100000; },
    (m: any) => { m.through_sequence = '01'; },
    (m: any) => { m.hand_id = 'table-1:0'; },
    (m: any) => { m.unknown = true; },
  ]) { const value = ready(); change(value); assert.throws(() => parseRecommendation(value)); }
  const empty = ready(); Object.assign(empty, { status: 'unavailable', reason: 'strategy_not_trained',
    actions: [], selected_action: null, model: null, expires_at_ms: empty.issued_at_ms });
  assert.equal(parseRecommendation(empty).actions.length, 0);
  empty.reason = 'strategy_feedback_unavailable';
  assert.equal(parseRecommendation(empty).actions.length, 0);
});

test('new local action, expiry, stop, lost history or changed turn immediately hides advice', async () => {
  const db = await database(), net = await db.create(1, false), session = await db.create(1, false, net.id);
  Object.assign(session, { last: '1', ack: '1' });
  const value = parseRecommendation(ready(session.id));
  const capture: CaptureState = { schema: 'tgto.capture-state/0.1', type: 'capture.state', projection_version: 'wpk-observation/1',
    analysis: { status: 'unsupported', reason: 'unverified_rules_and_strategy_coverage', actions: [], missing_fields: [] },
    source_id: session.id, through_sequence: '1', state: {
    hand_id: 'table-1:2', history_complete: true, closed: false, gap: false, turn: { player_id: 'hero' },
  } };
  assert.equal(currentRecommendation(value, session, capture), value);
  for (const change of [
    (s: any, _c: any) => { s.last = '2'; },
    (s: any, _c: any) => { s.ack = '0'; },
    (s: any, _c: any) => { s.state = 'stopped'; },
    (_s: any, c: any) => { c.state.hand_id = 'table-1:3'; },
    (_s: any, c: any) => { c.state.gap = true; },
    (_s: any, c: any) => { c.state.turn.player_id = 'player-1'; },
  ]) {
    const s = structuredClone(session), c = structuredClone(capture); change(s, c);
    assert.equal(currentRecommendation(value, s, c), undefined);
  }
  assert.equal(currentRecommendation(value, session, capture, value.expires_at_ms), undefined);
  db.db.close();
});

test('search provenance requires bounded completed work and explicit fallback diagnostics', () => {
  const searched = ready();
  searched.reason = 'searched_strategy'; searched.model.source = 'pluribus_search';
  searched.model.search = { status: 'complete', iterations: 20, nodes: 2000, rollout_nodes: 1000, root_draws: 160, elapsed_ms: 800, stop_reason: 'iterations' };
  assert.equal(parseRecommendation(searched).model?.source, 'pluribus_search');
  for (const change of [
    (m: any) => { delete m.model.search; },
    (m: any) => { m.model.search.iterations = 0; },
    (m: any) => { m.model.search.iterations = 101; },
    (m: any) => { m.model.search.nodes = 30001; },
    (m: any) => { m.model.search.root_draws = 0; },
    (m: any) => { m.model.search.elapsed_ms = -1; },
    (m: any) => { m.model.search.status = 'converged'; },
    (m: any) => { m.model.search.stop_reason = 'cancelled'; },
    (m: any) => { m.reason = 'covered_blueprint'; },
    (m: any) => { m.model.source = 'pluribus_blueprint'; m.reason = 'covered_blueprint'; },
  ]) { const value = structuredClone(searched); change(value); assert.throws(() => parseRecommendation(value)); }
  for (const status of ['coverage_missing', 'budget']) {
    const value = ready(); value.model.search = { status, elapsed_ms: 400 };
    assert.equal(parseRecommendation(value).model?.source, 'pluribus_blueprint');
    value.model.search.iterations = 1;
    assert.throws(() => parseRecommendation(value));
  }
});

test('wire recommendations require negotiation and ACK, ignore old versions and clear on disconnect', async () => {
  const db = await database(), net = await db.create(1, false), source = await db.create(1, false, net.id);
  const gap = { kind: 'capture_gap', table_id: null, hand_id: null, data: { reason: 'synthetic' } };
  await db.appendBusiness(source.id, [gap]);
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise<void>(resolve => server.once('listening', resolve));
  const port = (server.address() as { port: number }).port;
  globalThis.WebSocket = class extends WS { constructor(_url: string) { super(`ws://127.0.0.1:${port}`); } } as unknown as typeof WebSocket;
  let socket: WS, status = '';
  server.on('connection', ws => {
    socket = ws; ws.on('message', bytes => {
      const m = JSON.parse(bytes.toString());
      if (m.type === 'auth') {
        assert.ok(m.capabilities.includes('capture_recommendations_v1'));
        ws.send(JSON.stringify({ type: 'ready', schema_version: '1.2', capabilities: ['capture_events_v1', 'capture_recommendations_v1'] }));
      } else if (m.type === 'capture.append') {
        ws.send(JSON.stringify({ type: 'capture.ack', schema: 'tgto.capture/0.1', source_id: source.id, through_sequence: m.events.at(-1).sequence }));
        ws.send(JSON.stringify(ready(source.id, m.events.at(-1).sequence)));
      } else if (m.type === 'ping') ws.send('{"type":"pong"}');
    });
  });
  const client = new ResearchSocket(db, value => { status = value; });
  try {
    client.connect('ws://127.0.0.1:8765/v1/research/stream');
    await until(() => client.recommendations.has(source.id));
    client.recommendations.clear();
    await db.appendBusiness(source.id, [gap]);
    socket!.send(JSON.stringify(ready(source.id, '1')));
    let pong = false; client.pong = () => { pong = true; }; client.socket!.send('{"type":"ping"}');
    await until(() => pong); assert.equal(client.recommendations.size, 0);
    socket!.send(JSON.stringify(ready(source.id, '3')));
    await until(() => status === 'error'); assert.equal(client.recommendations.size, 0);
  } finally {
    client.stop(); for (const ws of server.clients) ws.terminate();
    await new Promise<void>(resolve => server.close(() => resolve())); db.db.close();
  }
});
