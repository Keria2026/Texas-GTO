import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import { installObserver } from '../../../adapters/wpk/observer.mjs';
import { ObservedSnapshotSchema, ResearchSocketEventSchema } from '../src/index.ts';

function page() {
  const emitted = [], g = { currentBoutNum: 1, round: 'FLOP', publicCards: [107, 209, 403], sitUserList: [
    { userId: 12345, seatNum: 1, currentScore: 800, handCards: [205, 210], nickname: 'private-name' },
    { userId: 67890, seatNum: 8, currentScore: 800, handCards: [101, 413], sessionToken: 'private-token' },
  ] };
  let calls = 0;
  const original = function (message) {
    calls++;
    if (message.msgType === 'WP_dealNotify') { g.currentBoutNum = message.msgBody.currentBoutNum; g.publicCards = []; g.round = 'PRE_FLOP'; }
    if (message.msgType === 'WP_roundChangeNotify') g.round = message.msgBody.round;
    return 17;
  };
  const client = { _dispatchMessageToCC: original };
  const window = { texasResearchEmit: text => emitted.push(JSON.parse(text)) };
  const scope = { window, CurrentUserInfo: { user: { userId: 12345 }, roomId: 987654 },
    HMFClient: { sharedInstance: () => client }, __require: () => ({ WPGameData: { Instance: g } }),
    setInterval: () => 1, clearInterval: () => {},
  };
  vm.runInNewContext(`(${installObserver.toString()})('synthetic-source')`, scope);
  return { emitted, g, client, window, original, user: scope.CurrentUserInfo, calls: () => calls };
}

test('read-only observer preserves the client call and exports only own visible cards', () => {
  const p = page();
  const first = ObservedSnapshotSchema.parse(p.emitted[0]);
  assert.deepEqual(first.hero_cards, ['5h', 'Th']);
  assert.equal(first.history_complete, false);
  assert.equal(JSON.stringify(first).includes('private'), false);
  assert.equal(JSON.stringify(first).includes('12345'), false);
  assert.equal(JSON.stringify(first).includes('As'), false);
  assert.equal(p.client._dispatchMessageToCC({ msgType: 'irrelevant' }), 17);
  assert.equal(p.calls(), 1);
  assert.equal(p.emitted.length, 1);
  p.window.__texasResearchObserver.stop();
  assert.equal(p.client._dispatchMessageToCC, p.original);
});

test('a new hand clears previous cards and retains fees observed before the deal', () => {
  const p = page();
  p.client._dispatchMessageToCC({ msgType: 'WP_bankerChangeNotify' });
  assert.deepEqual(p.emitted.at(-1).hero_cards, [null, null]);
  p.client._dispatchMessageToCC({ msgType: 'WP_actionNotify', msgBody: { actionList: [
    { actionId: 2001, userId: 12345, actionType: 'PAY_MUSHROOM', actionScore: 20 },
    { actionId: 2002, userId: 67890, actionType: 'PAY_MUSHROOM', actionScore: 20 },
  ] } });
  p.client._dispatchMessageToCC({ msgType: 'WP_dealNotify', msgBody: { currentBoutNum: 2 } });
  const current = ObservedSnapshotSchema.parse(p.emitted.at(-1));
  assert.equal(current.hand_id, 'table-1:2');
  assert.equal(current.history_complete, true);
  assert.deepEqual(current.actions.map(a => a.source_event_id), ['2001', '2002']);
  p.window.__texasResearchObserver.stop();
});

test('the wire contract rejects leaked credentials and malformed sequence values', () => {
  const p = page(), snapshot = p.emitted[0];
  assert.equal(ObservedSnapshotSchema.safeParse({ ...snapshot, sessionToken: 'forbidden' }).success, false);
  assert.equal(ObservedSnapshotSchema.safeParse({ ...snapshot, sequence: true }).success, false);
  assert.equal(ResearchSocketEventSchema.safeParse({ type: 'observation', active: true, state_version: 1, snapshot }).success, true);
  p.window.__texasResearchObserver.stop();
});

test('turn options are cleared after the actor acts and room changes reset history', () => {
  const p = page();
  p.client._dispatchMessageToCC({ msgType: 'WP_userOptNotify', msgBody: {
    userId: 12345, canActionList: ['FOLD','CALL','RAISE'], callScore: 8, minRaiseScore: 16, maxRaiseScore: 800,
  } });
  const turn = ObservedSnapshotSchema.parse(p.emitted.at(-1));
  assert.equal(turn.to_act_player_id, 'hero'); assert.equal(turn.call_amount, 8);
  assert.deepEqual(turn.legal_actions, ['FOLD','CALL','RAISE']);
  p.client._dispatchMessageToCC({ msgType: 'WP_actionNotify', msgBody: { actionList: [
    { actionId: 1001, userId: 12345, actionType: 'CALL', actionScore: 8, seatScore: 8 },
  ] } });
  assert.deepEqual(p.emitted.at(-1).legal_actions, []);
  p.user.roomId = 45678;
  p.client._dispatchMessageToCC({ msgType: 'C_updateRoomNotify' });
  assert.equal(p.emitted.at(-1).table_id, 'table-2');
  assert.deepEqual(p.emitted.at(-1).actions, []);
  assert.equal(p.emitted.at(-1).history_complete, false);
  p.window.__texasResearchObserver.stop();
});

test('showdown never exposes stale turn options carried by the round message', () => {
  const p = page(), turn = { userId: 12345, canActionList: ['CHECK','RAISE'], callScore: 0 };
  p.client._dispatchMessageToCC({ msgType: 'WP_userOptNotify', msgBody: turn });
  assert.equal(p.emitted.at(-1).to_act_player_id, 'hero');
  p.client._dispatchMessageToCC({ msgType: 'WP_roundChangeNotify', msgBody: { round: 'THAN', userAciton: turn } });
  const snapshot = ObservedSnapshotSchema.parse(p.emitted.at(-1));
  assert.equal(snapshot.street, 'showdown');
  assert.equal(snapshot.to_act_player_id, null); assert.deepEqual(snapshot.legal_actions, []);
  p.window.__texasResearchObserver.stop();
});
