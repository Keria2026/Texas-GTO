import test from 'node:test';
import assert from 'node:assert/strict';
import WS, { WebSocketServer } from 'ws';
import { core, database, until } from './helpers.ts';
import { parseCaptureState } from '../extension/capture-state.ts';
import { ResearchSocket } from '../extension/transport.ts';
import { fixture, normalize } from './wpk-fixture.ts';

function state(source: string, through = '1'): any {
  return { schema: 'tgto.capture-state/0.1', type: 'capture.state', source_id: source, through_sequence: through,
    projection_version: 'wpk-observation/1',
    state: { table_id: null, hand_id: null, street: 'unknown', hero_cards: [null, null], board: [], players: [],
      actions: [], turn: null, history_complete: false, closed: false, rules_version: 'wpk-observed-unverified-v1',
      gap: true, settlements: [], pending_settlements: [] },
    analysis: { status: 'unsupported', reason: 'unverified_rules_and_strategy_coverage', actions: [], missing_fields: ['verified_rule_profile'] } };
}
test('server observations validate schema, cards, scope, amounts and empty recommendations', () => {
  const message = state('synthetic'); assert.deepEqual(parseCaptureState(message, core), message);
  for (const mutate of [
    (m: any) => { m.analysis.actions = [{ action: 'CALL' }]; },
    (m: any) => { m.state.hero_cards = ['As', 'As']; },
    (m: any) => { m.state.players = [{ player_id: 'hero', seat: 1, stack: -1 }]; },
    (m: any) => { m.state.hand_id = 'table-2:1'; },
    (m: any) => { m.state.turn = { player_id: 'player-1', legal_actions: ['CALL'], call_amount: 2, min_raise_reported: null, max_raise_reported: null }; },
    (m: any) => { m.state.token = 'forbidden'; },
    (m: any) => { m.through_sequence = '01'; },
  ]) { const bad = structuredClone(message); mutate(bad); assert.throws(() => parseCaptureState(bad, core)); }
});
test('street transitions and each new hand emit complete replayable snapshots', () => {
  const f = fixture(); f.fixture.play(); const { events } = normalize(f.messages);
  const snapshots = events.filter(e => e.kind === 'snapshot');
  for (const street of ['preflop', 'flop', 'turn', 'river', 'showdown', 'between_hands'])
    assert.ok(snapshots.some(e => e.data.street === street), street);
  for (const hand of ['table-1:2', 'table-1:3'])
    assert.ok(snapshots.some(e => e.hand_id === hand && e.data.players.length === 3));
});
test('capture state is negotiated, source-bound, ACK-bound and cleared on disconnect', async () => {
  const db = await database(), net = await db.create(1, false), business = await db.create(1, false, net.id);
  const gap = { kind: 'capture_gap', table_id: null, hand_id: null, data: { reason: 'synthetic' } };
  await db.appendBusiness(business.id, [gap]);
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise<void>(resolve => server.once('listening', resolve));
  const port = (server.address() as { port: number }).port;
  globalThis.WebSocket = class extends WS {
    constructor(_url: string) { super(`ws://127.0.0.1:${port}/v1/research/stream`); }
  } as unknown as typeof WebSocket;
  let socket: WS; let transport = '';
  server.on('connection', ws => {
    socket = ws; ws.on('message', bytes => {
      const m = JSON.parse(bytes.toString());
      if (m.type === 'auth') {
        assert.ok(m.capabilities.includes('capture_state_v1'));
        ws.send(JSON.stringify({ type: 'ready', schema_version: '1.2', capabilities: ['capture_events_v1', 'capture_state_v1'] }));
      }
      if (m.type === 'capture.append') {
        ws.send(JSON.stringify({ type: 'capture.ack', schema: 'tgto.capture/0.1', source_id: business.id, through_sequence: m.events.at(-1).sequence }));
        ws.send(JSON.stringify(state(business.id, m.events.at(-1).sequence)));
      }
    });
  });
  const client = new ResearchSocket(db, s => { transport = s; });
  try {
    client.connect('ws://127.0.0.1:8766/v1/research/stream', 'synthetic-test-token');
    await until(() => client.captureStates.has(business.id));
    assert.equal(client.captureStates.get(business.id)?.through_sequence, '1');
    client.captureStates.clear();
    await db.appendBusiness(business.id, [gap]);
    socket!.send(JSON.stringify(state(business.id, '1')));
    // Ping is serialized behind the stale state, so receiving pong proves it was processed.
    let pong = false; client.pong = () => { pong = true; }; client.socket!.send('{"type":"ping"}');
    socket!.send('{"type":"pong"}'); await until(() => pong);
    assert.equal(client.captureStates.size, 0);
    socket!.send(JSON.stringify(state(business.id, '3')));
    await until(() => transport === 'error');
    assert.equal(client.captureStates.size, 0);
    assert.equal((await db.sessions()).find(s => s.id === business.id)!.ack, '1');
  } finally { client.stop(); for (const ws of server.clients) ws.terminate(); await new Promise<void>(r => server.close(() => r())); db.db.close(); }
});
