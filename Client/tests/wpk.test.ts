import test from 'node:test';
import assert from 'node:assert/strict';
import { wpkCall } from '../extension/core.ts';
import { core, database } from './helpers.ts';
import { WpkAdapter } from '../extension/wpk.ts';
import type { Target } from '../extension/capture.ts';
import { fixture, normalize } from './wpk-fixture.ts';
test('real WASM normalizes all streets, pending fees, confirmed actions and late paired settlement', () => {
  const f = fixture(); f.fixture.play(); const result = normalize(f.messages);
  const actions = result.events.filter(e => e.kind === 'action_confirmed');
  assert.equal(actions.length, 15);
  assert.deepEqual(actions.slice(0, 3).map(e => e.hand_id), ['table-1:2', 'table-1:2', 'table-1:2']);
  assert.ok(actions.some(e => e.data.action === 'FOLD')); assert.ok(actions.some(e => e.data.action === 'RAISE'));
  assert.ok(result.events.some(e => e.kind === 'hero_cards' && e.data.cards[0] === '5h'));
  assert.ok(result.events.some(e => e.kind === 'board_updated' && e.data.cards.length === 5));
  const settlement = result.events.find(e => e.kind === 'settlement');
  assert.equal(settlement.hand_id, 'table-1:2'); assert.equal(settlement.data.status, 'reconciled');
  assert.equal(settlement.data.pool_change, 60); assert.equal(settlement.data.players[0].net_chips, 16);
  assert.deepEqual(settlement.data.hero_cards, ['5h', 'Th']);
  assert.equal(result.checkpoint.hand_number, 3); assert.equal(result.checkpoint.turn, null); assert.equal(result.checkpoint.closed, true);
  const serialized = JSON.stringify({ messages: f.messages, result });
  for (const secret of ['TOP_SECRET_FORBIDDEN', 'PRIVATE_NICKNAME', 'DO_NOT_READ', '912345', '967890', '223344']) assert.ok(!serialized.includes(secret));
});
test('page observer preserves original return/throw, polls async decoded cards and removes itself', () => {
  const f = fixture(true);
  assert.equal(f.fixture.emit('IRRELEVANT'), 17); assert.equal(f.fixture.calls(), 1);
  assert.throws(() => f.fixture.emit('SYNTHETIC_THROW'), e => e === f.fixture.failure);
  f.fixture.game.sitUserList[0].handCards = [101, 413]; f.timers[0]();
  assert.deepEqual(normalize(f.messages).checkpoint.hero_cards, ['As', 'Kd']);
  f.scope.__tgtoWpkBridge.stop(); assert.equal(f.fixture.client._dispatchMessageToCC, f.fixture.original);
});
test('repeated actions dedupe, enrichment is explicit and conflicting/unknown actions fail', () => {
  const f = fixture(); f.fixture.emit('WP_actionNotify', { actionList: [{ actionId: 1, userId: f.fixture.own, actionType: 'CALL', actionScore: 8, seatScore: 8 }] });
  const dto = f.messages.at(-1).dto; const first = normalize(f.messages);
  const again = wpkCall(core, { op: 'reduce', message: dto, checkpoint: first.checkpoint });
  assert.equal(again.events.length, 0);
  assert.throws(() => wpkCall(core, { op: 'reduce', message: { ...dto, actions: [{ ...dto.actions[0], amount_delta: 9 }] }, checkpoint: first.checkpoint }), /Conflicting/);
  assert.throws(() => wpkCall(core, { op: 'reduce', message: { ...dto, actions: [{ ...dto.actions[0], action: 'UNKNOWN' }] }, checkpoint: first.checkpoint }), /Invalid action/);
  assert.throws(() => wpkCall(core, { op: 'reduce', message: { ...dto, actions: [{ ...dto.actions[0], amount_delta: 9007199254740992 }] }, checkpoint: first.checkpoint }), /Invalid action amount/);
});
test('malformed cards, unsafe amounts and private fields fail in WASM', () => {
  const f = fixture(); const dto = f.messages.find(m => m.dto).dto;
  for (const patch of [{ hero_cards: [101, 101] }, { board: [101, 202] }, { hero_cards: [999, 210] }]) {
    assert.throws(() => wpkCall(core, { op: 'reduce', message: { ...dto, visible: { ...dto.visible, ...patch } }, checkpoint: null }));
  }
  assert.throws(() => wpkCall(core, { op: 'reduce', message: { ...dto, token: 'forbidden' }, checkpoint: null }));
  const event = { schema: 'tgto.capture/0.1', type: 'business_event', adapter_version: 'wpk/1', source_id: 'synthetic', sequence: '1', captured_at: new Date().toISOString(), table_id: 'table-1', hand_id: 'table-1:1', kind: 'hero_cards', data: { cards: ['é'] } };
  assert.throws(() => wpkCall(core, { op: 'validate', record: event }), /Invalid/);
  assert.throws(() => wpkCall(core, { op: 'validate', record: { ...event, data: { cards: ['As', 'Kd'], sessionToken: 'forbidden' } } }));
  assert.throws(() => wpkCall(core, { op: 'validate', record: { ...event, table_id: null, hand_id: null, data: { cards: ['As', 'Kd'] } } }), /scope/);
});
test('missing pair stays pending, missing amounts/differences are unresolved, conflicts reject', () => {
  const f = fixture(); f.fixture.play(); const dtos = f.messages.filter(m => m.dto).map(m => m.dto);
  const beforeLog = dtos.slice(0, dtos.findIndex(d => d.event === 'C_playLogNotify'));
  const before = normalize(beforeLog.map(dto => ({ dto })));
  assert.ok(!before.events.some(e => e.kind === 'settlement')); assert.equal(before.checkpoint.results.length, 1);
  const log = dtos.find(d => d.event === 'C_playLogNotify');
  assert.throws(() => wpkCall(core, { op: 'reduce', message: { ...log, log: { ...log.log, players: log.log.players.map((p: any, i: number) => i ? p : { ...p, net_chips: 99 }) } }, checkpoint: before.checkpoint }), /disagree/);
  const missing = beforeLog.map(d => d.result ? { ...d, result: { ...d.result, players: d.result.players.map((p: any) => ({ ...p, mushroom_awarded: null })) } } : d);
  const pending = normalize(missing.map(dto => ({ dto })));
  const r = wpkCall(core, { op: 'reduce', message: log, checkpoint: pending.checkpoint });
  assert.equal((r.events.find(e => e.kind === 'settlement')!.data as any).status, 'unresolved');
  const differenceDtos = dtos.map(d => ({ ...d,
    result: d.result ? { ...d.result, players: d.result.players.map((p: any) => ({ ...p, mushroom_awarded: 1 })) } : null }));
  const difference = normalize(differenceDtos.map(dto => ({ dto })));
  assert.equal(difference.events.find(e => e.kind === 'settlement').data.reason, 'point_difference');
  const done = normalize(f.messages);
  assert.equal(wpkCall(core, { op: 'reduce', message: log, checkpoint: done.checkpoint }).events.filter(e => e.kind === 'settlement').length, 0);
  assert.throws(() => wpkCall(core, { op: 'reduce', message: { ...log, log: { ...log.log, players: log.log.players.map((p: any, i: number) => i ? p : { ...p, net_chips: 99 }) } }, checkpoint: done.checkpoint }), /immutable/);
});

test('only the bound main-world context is accepted; invalid business DTO stops business only', async () => {
  const db = await database(); const network = await db.create(7, false);
  const api = { async attach() {}, async detach() {}, async sendCommand(_t: Target, method: string) {
    return method === 'Page.getFrameTree' ? { frameTree: { frame: { id: 'main' } } } : {};
  } };
  const adapter = new WpkAdapter(api, db, core, () => {}); await adapter.start(network);
  const source = adapter.source!.id;
  const dto = fixture().messages.find(m => m.dto).dto;
  await adapter.handle({ tabId: 7 }, 'Runtime.executionContextCreated', { context: { id: 11, auxData: { isDefault: true, frameId: 'main' } } });
  await adapter.handle({ tabId: 7 }, 'Runtime.bindingCalled', { name: adapter.binding, executionContextId: 99, payload: 'invalid' });
  assert.equal((await db.records(source)).length, 1);
  await adapter.handle({ tabId: 7 }, 'Runtime.bindingCalled', { name: adapter.binding, executionContextId: 11, payload: JSON.stringify({ dto }) });
  assert.equal(adapter.status, 'ready');
  await adapter.handle({ tabId: 7 }, 'Runtime.bindingCalled', { name: adapter.binding, executionContextId: 11, payload: JSON.stringify({ dto: { ...dto, credential: 'forbidden' } }) });
  assert.equal(adapter.status, 'interrupted');
  assert.equal((await db.sessions()).find(s => s.id === network.id)!.state, 'active');
  assert.equal((await db.sessions()).find(s => s.id === source)!.state, 'interrupted'); db.db.close();
});

test('failed multi-event transaction rolls back the business checkpoint and all emitted events', async () => {
  const db = await database(800); const network = await db.create(1, false), source = await db.create(1, false, network.id);
  const gap = { kind: 'capture_gap', table_id: null, hand_id: null, data: { reason: 'x'.repeat(500) } };
  await assert.rejects(db.appendBusiness(source.id, [gap, gap], { hand_number: 99 }), /limit/);
  const state = (await db.sessions()).find(s => s.id === source.id)!;
  assert.equal(state.last, '0'); assert.equal(state.checkpoint, undefined); assert.equal((await db.records(source.id)).length, 0); db.db.close();
});
test('business state and events commit together; independent stream survives reopen and ACK', async () => {
  const db = await database(); const net = await db.create(1, false), business = await db.create(1, false, net.id);
  const f = fixture(); const reduced = normalize(f.messages);
  await db.appendBusiness(business.id, reduced.events, reduced.checkpoint);
  assert.equal((await db.sessions()).find(s => s.id === business.id)?.checkpoint?.hand_number, 1);
  assert.equal((await db.sessions()).find(s => s.id === net.id)?.last, '0');
  const records = await db.records(business.id); assert.equal(records[0].record.schema, 'tgto.capture/0.1');
  await db.acknowledge(business.id, String(records.length), String(records.length));
  assert.equal(await db.batch(), null); assert.equal((await db.records(business.id)).length, records.length); db.db.close();
});
