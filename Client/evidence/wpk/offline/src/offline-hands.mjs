const STREETS = { BET_BLIND: 'preflop', PRE_FLOP: 'preflop', FLOP: 'flop', TURN: 'turn', RIVER: 'river', THAN: 'showdown' };
const DEBITS = new Set(['SMALL_BLIND', 'BIG_BLIND', 'THIRD_BLIND', 'ANTE', 'BET', 'RAISE', 'CALL', 'ALL_IN']);

export function card(code) {
  if (code === null || code === -1) return null;
  const suit = Math.trunc(code / 100), rank = code % 100;
  if (!Number.isInteger(code) || suit < 1 || suit > 4 || rank < 1 || rank > 13) throw new Error('Invalid card code');
  return ' A23456789TJQK'[rank] + ' shcd'[suit];
}

function integer(value, label) {
  if (!Number.isSafeInteger(value)) throw new Error('Missing or unsafe integer: ' + label);
  return value;
}

function identifier(value) {
  if (typeof value === 'number') integer(value, 'identifier');
  if (!/^[0-9]+$/.test(String(value))) throw new Error('Invalid identifier');
  return String(value);
}

function cards(value) {
  const list = typeof value === 'string' ? JSON.parse(value) : value;
  if (!Array.isArray(list)) throw new Error('Card array required');
  return list.map(card);
}

function uniqueCards(list) {
  const known = list.filter(c => c !== null);
  if (new Set(known).size !== known.length) throw new Error('Duplicate known card');
}

export function exportHands(records, heroUserId) {
  const hero = identifier(heroUserId);
  const valid = records.filter(r => r.message && r.direction === 'received');
  const deals = valid.filter(r => r.message.msgType === 'WP_dealNotify');
  const logs = valid.filter(r => r.message.msgType === 'C_playLogNotify');
  const boundaries = valid.filter(r => r.message.msgType === 'WP_bankerChangeNotify');
  const aliases = new Map([[hero, 'hero']]);
  function alias(id) {
    const key = identifier(id);
    if (!aliases.has(key)) aliases.set(key, 'player-' + aliases.size);
    return aliases.get(key);
  }
  const tables = new Map();
  function tableAlias(id) {
    const key = identifier(id);
    if (!tables.has(key)) tables.set(key, 'table-' + (tables.size + 1));
    return tables.get(key);
  }
  const latestRoom = valid.find(r => r.message.msgType === 'C_updateRoomNotify')?.message.msgBody;
  let previousEnd = -Infinity;
  const completed = [];
  for (const log of logs) {
    const body = log.message.msgBody;
    if (body.handList2?.length) throw new Error('Two-board settlement is outside this sample');
    if (!body.handList1?.length) throw new Error('Settlement player list is missing');
    const handNumber = integer(body.handNum, 'handNum');
    const roomId = identifier(body.handList1[0].roomId);
    if (body.handList1.some(p => identifier(p.roomId) !== roomId)) throw new Error('Mixed rooms in a settlement');
    if (completed.some(h => h.sourceHandNumber === handNumber && h.tableId === tableAlias(roomId))) throw new Error('Duplicate hand settlement');
    const end = Date.parse(log.time);
    if (!Number.isFinite(end) || end <= previousEnd) throw new Error('Settlement order is not increasing');
    const matchingDeals = deals.filter(r => r.message.msgBody.currentBoutNum === handNumber && Date.parse(r.time) <= end);
    if (matchingDeals.length !== 1) throw new Error('A completed hand requires exactly one matching deal');
    const dealTime = Date.parse(matchingDeals[0].time);
    const boundary = boundaries.findLast(r => Date.parse(r.time) <= dealTime);
    if (!boundary) throw new Error('Hand start boundary is missing');
    const begin = Date.parse(boundary.time);
    const next = boundaries.find(r => Date.parse(r.time) > begin);
    const nextStart = next ? Date.parse(next.time) : Infinity;
    // The next hand's fee can arrive before the previous hand's final log.
    const segment = valid.filter(r => Date.parse(r.time) >= begin && Date.parse(r.time) < nextStart);
    const ownDeal = matchingDeals[0].message.msgBody.list.find(p => identifier(p.userId) === hero);
    const heroCards = ownDeal ? cards(ownDeal.handCards) : [];
    if (heroCards.length !== 2 || heroCards.includes(null)) throw new Error('Own hole cards were not decoded');
    let street = 'preflop';
    const actionsById = new Map(), boards = [];
    let board = [];
    for (const record of segment) {
      const m = record.message, b = m.msgBody;
      if (m.msgType === 'WP_roundChangeNotify') {
        street = STREETS[b.round];
        if (!street) throw new Error('Unknown street: ' + b.round);
        const added = cards(b.dealPublicCards ?? []);
        if (added.length) {
          board = [...board, ...added];
          if (board.length > 5 || board.includes(null)) throw new Error('Invalid public board');
          uniqueCards([...board, ...heroCards]);
          boards.push({ street, cards: [...board], addedCards: added, time: record.time });
        }
      }
      if (m.msgType === 'WP_actionNotify' || m.msgType === 'WP_actionHistoryNotify') for (const a of b.actionList) {
        const sourceId = integer(a.actionId, 'actionId');
        const amount = integer(a.actionScore, 'actionScore');
        const existing = actionsById.get(sourceId);
        if (existing) {
          if (existing.type !== a.actionType || existing.playerId !== alias(a.userId) || existing.amountReported !== amount) throw new Error('Conflicting source action ID');
          if (m.msgType === 'WP_actionHistoryNotify') continue;
        }
        const player = body.handList1.find(p => identifier(p.userId) === identifier(a.userId));
        if (!player) throw new Error('Action player is missing from hand history');
        actionsById.set(sourceId, {
          eventId: `${tableAlias(roomId)}:${handNumber}:${sourceId}`,
          sourceEventId: String(sourceId),
          playerId: alias(a.userId), seat: integer(a.seatNum ?? player.seatNum, 'seatNum'),
          street: a.round && a.round !== 'CLEAN' ? STREETS[a.round] : street,
          type: a.actionType, amountDelta: DEBITS.has(a.actionType) ? amount : a.actionType === 'PAY_MUSHROOM' ? amount : null,
          amountReported: amount,
          amountTo: DEBITS.has(a.actionType) && a.seatScore !== undefined ? integer(a.seatScore, 'seatScore') : null,
          balanceAfter: a.currentScore === undefined ? null : integer(a.currentScore, 'currentScore'),
          source: m.msgType === 'WP_actionHistoryNotify' ? 'history-backfill' : 'notification', time: record.time
        });
      }
    }
    const actions = [...actionsById.values()].sort((a, b) => Number(a.sourceEventId) - Number(b.sourceEventId));
    for (let i = 0; i < actions.length; i++) {
      if (i && Number(actions[i].sourceEventId) !== Number(actions[i - 1].sourceEventId) + 1) throw new Error('Source action sequence has an unfilled gap');
      if (!actions[i].street) throw new Error('Unknown action street');
      actions[i].seq = i + 1;
    }
    const finalBoard = cards(body.handList1[0].publicCardAll);
    if (![0, 3, 4, 5].includes(finalBoard.length) || finalBoard.includes(null)) throw new Error('Invalid final board');
    if (JSON.stringify(board) !== JSON.stringify(finalBoard)) throw new Error('Board stream disagrees with the final hand log');
    const result = segment.find(r => r.message.msgType === 'WP_playResultNotify');
    if (!result) throw new Error('Final settlement notification is missing');
    const players = body.handList1.map(p => {
      const id = identifier(p.userId);
      const settlement = result.message.msgBody.thanList.find(row => identifier(row.userId) === id);
      if (!settlement) throw new Error('Player is missing from settlement');
      const revealedCards = id === hero ? [] : cards(p.handCard);
      if (id === hero && JSON.stringify(cards(p.handCard)) !== JSON.stringify(heroCards)) throw new Error('Own cards disagree with the final hand log');
      uniqueCards([...finalBoard, ...heroCards, ...revealedCards.filter(c => c !== null)]);
      return { playerId: alias(id), seat: integer(p.seatNum, 'seatNum'), position: p.seatPos,
        revealedCards, revealedAt: 'settlement', netChips: integer(p.changeScore, 'changeScore'),
        mushroomPaid: integer(p.mushroomPayScore, 'mushroomPayScore'),
        mushroomAwarded: integer(p.mushroomScore ?? settlement.mushroomScore, 'mushroomScore'),
        cardType: p.cardType ?? null };
    });
    uniqueCards([...finalBoard, ...heroCards, ...players.flatMap(p => p.revealedCards)]);
    for (const p of players) {
      const fee = actions.filter(a => a.playerId === p.playerId && a.type === 'PAY_MUSHROOM').reduce((s, a) => s + a.amountDelta, 0);
      if (fee !== p.mushroomPaid) throw new Error('Fee actions disagree with settlement');
    }
    const paid = players.reduce((s, p) => s + p.mushroomPaid, 0);
    const awarded = players.reduce((s, p) => s + p.mushroomAwarded, 0);
    const netSum = players.reduce((s, p) => s + p.netChips, 0);
    const poolChange = paid - awarded;
    if (netSum + poolChange !== 0) throw new Error('Player changes and mushroom pool do not conserve chips');
    for (const p of result.message.msgBody.thanList) {
      const player = players.find(row => row.playerId === alias(p.userId));
      if (!player || integer(p.addScore, 'addScore') !== player.netChips) throw new Error('Settlement disagrees with final hand log');
    }
    completed.push({
      handId: `${tableAlias(roomId)}:${handNumber}`, tableId: tableAlias(roomId), sourceHandNumber: handNumber,
      status: 'settled', heroId: 'hero', heroCards, board: finalBoard, boards, players, actions,
      totalPotReportedByHistory: integer(body.totalPot, 'totalPot'),
      mushroom: { paid, awarded, poolChange }, settledAt: log.time,
      evidence: ['WP_dealNotify', 'WP_roundChangeNotify', 'WP_actionNotify', 'WP_playResultNotify', 'C_playLogNotify']
    });
    previousEnd = end;
  }
  const unfinished = deals.filter(d => Date.parse(d.time) > previousEnd).map(d => {
    const own = d.message.msgBody.list.find(p => identifier(p.userId) === hero);
    return { sourceHandNumber: d.message.msgBody.currentBoutNum, status: 'incomplete',
      heroCards: own ? cards(own.handCards) : [], settlement: null };
  });
  return { format: 'wpk-offline-review-v1', purpose: 'completed-hand-review',
    observedTable: latestRoom ? { capacity: latestRoom.gamePersonNum, participantsInFirstCompletedHand: completed[0]?.players.length,
      smallBlind: latestRoom.gradeCfg.smallBlind, bigBlind: latestRoom.gradeCfg.bigBlind,
      thirdBlindEnabled: Boolean(latestRoom.isThirdBlind), ante: latestRoom.ante } : null,
    completed, unfinished,
    rejectedFrames: records.filter(r => r.error).map(r => ({ time: r.time, direction: r.direction, reason: 'decode-failed' })) };
}
