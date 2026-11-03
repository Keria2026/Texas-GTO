import test from 'node:test';
import assert from 'node:assert/strict';
import { card, exportHands } from '../src/offline-hands.mjs';

function fixture() {
  const r = (n, msgType, msgBody) => ({ time: new Date(n * 1000).toISOString(), direction: 'received', message: { msgType, msgBody } });
  const a = (actionId, userId, actionType, actionScore, seatScore, currentScore) => ({ actionId, userId, seatNum: userId, actionType, actionScore, seatScore, currentScore });
  return [
    r(0, 'C_updateRoomNotify', { roomId: 99, gamePersonNum: 8, gradeCfg: { smallBlind: 1, bigBlind: 2 }, isThirdBlind: 0, ante: 0 }),
    r(1, 'WP_bankerChangeNotify', {}),
    r(2, 'WP_actionNotify', { actionList: [a(101, 1, 'PAY_MUSHROOM', 20, 0, 80), a(102, 2, 'PAY_MUSHROOM', 20, 0, 80)] }),
    r(3, 'WP_actionNotify', { actionList: [a(103, 1, 'SMALL_BLIND', 1, 1, 79), a(104, 2, 'BIG_BLIND', 2, 2, 78)] }),
    r(4, 'WP_dealNotify', { currentBoutNum: 1, list: [{ userId: 1, handCards: [205, 210] }, { userId: 2, handCards: [-1, -1] }] }),
    r(5, 'WP_roundChangeNotify', { round: 'PRE_FLOP', dealPublicCards: [] }),
    r(6, 'WP_actionNotify', { actionList: [a(105, 1, 'CALL', 1, 2, 78)] }),
    r(7, 'WP_actionNotify', { actionList: [a(106, 2, 'CHECK', 0, 2, 78)] }),
    r(8, 'WP_roundChangeNotify', { round: 'FLOP', dealPublicCards: [107, 209, 403] }),
    r(9, 'WP_actionNotify', { actionList: [a(107, 2, 'FOLD', 0, 0, 78)] }),
    r(10, 'WP_playResultNotify', { thanList: [{ userId: 1, addScore: -18, mushroomScore: 0 }, { userId: 2, addScore: -22, mushroomScore: 0 }] }),
    r(11, 'WP_bankerChangeNotify', {}),
    r(12, 'WP_actionNotify', { actionList: [a(201, 1, 'PAY_MUSHROOM', 20, 0, 62), a(202, 2, 'PAY_MUSHROOM', 20, 0, 58)] }),
    r(13, 'C_playLogNotify', { handNum: 1, totalPot: 4, handList2: [], handList1: [
      { userId: 1, roomId: 99, seatNum: 1, seatPos: 'SMALL_BLIND', publicCardAll: '[107,209,403]', handCard: '[205,210]', changeScore: -18, mushroomPayScore: 20 },
      { userId: 2, roomId: 99, seatNum: 2, seatPos: 'BIG_BLIND', publicCardAll: '[107,209,403]', handCard: '[-1,-1]', changeScore: -22, mushroomPayScore: 20 }
    ] }),
    r(14, 'WP_dealNotify', { currentBoutNum: 2, list: [{ userId: 1, handCards: [113, 202] }] })
  ];
}

test('card encoding and hidden cards', () => {
  assert.equal(card(101), 'As'); assert.equal(card(210), 'Th');
  assert.equal(card(413), 'Kd'); assert.equal(card(-1), null);
  assert.throws(() => card(114), /Invalid card/);
});

test('late final log cannot attach next-hand fees to the previous hand', () => {
  const out = exportHands(fixture(), '1'), h = out.completed[0];
  assert.deepEqual(h.heroCards, ['5h', 'Th']);
  assert.deepEqual(h.board, ['7s', '9h', '3d']);
  assert.equal(h.actions.length, 7);
  assert.deepEqual(h.mushroom, { paid: 40, awarded: 0, poolChange: 40 });
  assert.deepEqual(h.players[1].revealedCards, [null, null]);
  assert.equal(h.actions.find(a => a.type === 'CALL').amountDelta, 1);
  assert.equal(h.actions.find(a => a.type === 'CALL').amountTo, 2);
  assert.equal(out.unfinished[0].settlement, null);
  assert.equal(out.unfinished[0].status, 'incomplete');
  assert.equal(h.tableId, 'table-1'); assert.equal(h.players[0].playerId, 'hero');
});

test('history backfills missing action without inventing its balance or target', () => {
  const input = fixture().filter(r => !r.message.msgBody.actionList?.some(a => a.actionId === 105));
  input.splice(8, 0, { time: new Date(9500).toISOString(), direction: 'received', message: {
    msgType: 'WP_actionHistoryNotify', msgBody: { actionList: [{ actionId: 105, userId: 1, actionType: 'CALL', actionScore: 1, round: 'PRE_FLOP' }] }
  } });
  const action = exportHands(input, '1').completed[0].actions.find(a => a.sourceEventId === '105');
  assert.equal(action.seq, 5); assert.equal(action.source, 'history-backfill');
  assert.equal(action.balanceAfter, null); assert.equal(action.amountTo, null);
});

test('missing actions, conflicting duplicates, unsafe amounts and duplicate cards are rejected', () => {
  const missing = fixture().filter(r => !r.message.msgBody.actionList?.some(a => a.actionId === 105));
  assert.throws(() => exportHands(missing, '1'), /unfilled gap/);
  const duplicate = fixture(); duplicate[6].message.msgBody.actionList.push({ ...duplicate[6].message.msgBody.actionList[0], actionScore: 2 });
  assert.throws(() => exportHands(duplicate, '1'), /Conflicting/);
  const unsafe = fixture(); unsafe[6].message.msgBody.actionList[0].actionScore = Number.MAX_SAFE_INTEGER + 1;
  assert.throws(() => exportHands(unsafe, '1'), /unsafe integer/);
  const cards = fixture(); cards[8].message.msgBody.dealPublicCards[0] = 205;
  assert.throws(() => exportHands(cards, '1'), /Duplicate known card/);
});

test('settlement, board and fee disagreements fail instead of generating a trusted hand', () => {
  const money = fixture(); money[13].message.msgBody.handList1[0].changeScore = -17;
  assert.throws(() => exportHands(money, '1'), /conserve/);
  const board = fixture(); board[13].message.msgBody.handList1[0].publicCardAll = '[107,209,404]';
  assert.throws(() => exportHands(board, '1'), /Board stream/);
  const fee = fixture(); fee[13].message.msgBody.handList1[0].mushroomPayScore = 21;
  assert.throws(() => exportHands(fee, '1'), /Fee actions/);
});
