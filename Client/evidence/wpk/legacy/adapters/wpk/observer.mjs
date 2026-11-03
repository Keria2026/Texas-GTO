// Runs inside the user's existing WPK page; backend credentials stay in the collector.
export function installObserver(sourceId) {
  if (window.__texasResearchObserver) throw new Error('An observer is already installed');
  const client = HMFClient.sharedInstance(), original = client._dispatchMessageToCC;
  const game = () => __require('WPGameData').WPGameData.Instance;
  const aliases = new Map(), tables = new Map();
  let sequence = 0, previous = '', pending = false, sawStart = false, complete = false;
  let actions = new Map(), pendingActions = new Map(), participants = new Map();
  let turn = null;
  let lastBout = game().currentBoutNum;
  const ownId = () => String(CurrentUserInfo.user.userId);
  const initialOwnId = ownId();
  let lastRoom = String(CurrentUserInfo.roomId);
  const alias = (id) => {
    id = String(id);
    if (id === ownId()) return 'hero';
    if (!aliases.has(id)) aliases.set(id, 'player-' + (aliases.size + 1));
    return aliases.get(id);
  };
  function card(code) {
    if (code === undefined || code === null || code === -1 || code === 0) return null;
    const suit = Math.trunc(code / 100), rank = code % 100;
    if (!Number.isInteger(code) || suit < 1 || suit > 4 || rank < 1 || rank > 13) throw new Error('Unknown card encoding');
    return ' A23456789TJQK'[rank] + ' shcd'[suit];
  }
  function emit() {
    const g = game(), room = String(CurrentUserInfo.roomId);
    if (ownId() !== initialOwnId) throw new Error('Account changed; restart the observer');
    if (room !== lastRoom) {
      lastRoom = room; lastBout = g.currentBoutNum; actions = new Map(); participants = new Map();
      pendingActions = new Map(); pending = false; sawStart = false; complete = false; turn = null;
    }
    const people = (g.sitUserList ?? []).filter(p => p.userId && p.seatNum >= 1 && p.seatNum <= 8);
    const mine = people.find(p => String(p.userId) === ownId());
    if (!mine) {
      if (sequence > 0) throw new Error('Own seat is no longer observable');
      return;
    }
    if (!g.currentBoutNum) return;
    if (!pending && g.currentBoutNum !== lastBout) {
      lastBout = g.currentBoutNum; actions = new Map(); participants = new Map(); complete = false; turn = null;
    }
    if (!tables.has(room)) tables.set(room, 'table-' + (tables.size + 1));
    for (const p of people) participants.set(alias(p.userId), {
      player_id: alias(p.userId), seat: p.seatNum,
      stack: Number.isSafeInteger(p.currentScore) && p.currentScore >= 0 ? p.currentScore : null,
    });
    const streets = { BET_BLIND: 'preflop', PRE_FLOP: 'preflop', FLOP: 'flop', TURN: 'turn', RIVER: 'river', THAN: 'showdown', CLEAN: 'between_hands' };
    const street = pending ? 'between_hands' : streets[g.round] ?? 'unknown';
    const between = street === 'between_hands';
    const heroCards = between ? [null, null] : [card(mine.handCards?.[0]), card(mine.handCards?.[1])];
    const board = between ? [] : (g.publicCards ?? []).map(card);
    const ordered = [...actions.values()].sort((a, b) => Number(a.source_event_id) - Number(b.source_event_id));
    const continuous = ordered.every((a, i) => !i || BigInt(a.source_event_id) === BigInt(ordered[i - 1].source_event_id) + 1n);
    const betting = ['preflop','flop','turn','river'].includes(street);
    const ownTurn = betting && turn && String(turn.userId) === ownId();
    const numeric = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
    const snapshot = {
      source_id: sourceId, table_id: tables.get(room),
      street,
      hand_id: `${tables.get(room)}:${pending ? 'pending-' + (g.currentBoutNum + 1) : g.currentBoutNum}`,
      hero_id: 'hero', hero_cards: heroCards, board,
      players: [...participants.values()], actions: pending ? [] : ordered,
      to_act_player_id: betting && turn?.userId ? alias(turn.userId) : null,
      legal_actions: ownTurn ? turn.canActionList ?? [] : [],
      call_amount: ownTurn ? numeric(turn.callScore) : null,
      min_raise_reported: ownTurn ? numeric(turn.minRaiseScore) : null,
      max_raise_reported: ownTurn ? numeric(turn.maxRaiseScore) : null,
      history_complete: !pending && complete && continuous,
      rules_version: 'wpk-observed-unverified-v1',
    };
    const fingerprint = JSON.stringify(snapshot);
    if (fingerprint === previous) return;
    previous = fingerprint;
    window.texasResearchEmit(JSON.stringify({ ...snapshot, sequence: ++sequence }));
  }
  function capture(message) {
    const type = message?.msgType, body = message?.msgBody;
    if (type === 'WP_bankerChangeNotify') {
      pending = true; sawStart = true; complete = false; pendingActions = new Map(); participants = new Map(); turn = null;
    }
    if (type === 'WP_dealNotify') {
      actions = pending ? pendingActions : new Map();
      lastBout = body.currentBoutNum;
      complete = sawStart; pending = false; sawStart = false; participants = new Map();
    }
    if (type === 'WP_actionNotify' || type === 'WP_actionHistoryNotify') {
      const destination = pending ? pendingActions : actions;
      for (const a of body?.actionList ?? []) {
        if (type === 'WP_actionNotify' && turn && String(a.userId) === String(turn.userId)) turn = null;
        if (!['PAY_MUSHROOM','SMALL_BLIND','BIG_BLIND','THIRD_BLIND','ANTE','BET','RAISE','CALL','ALL_IN','FOLD','CHECK'].includes(a.actionType)) continue;
        const id = String(a.actionId), value = {
          source_event_id: id, player_id: alias(a.userId), action: a.actionType,
          amount_delta: a.actionScore,
          amount_to: ['PAY_MUSHROOM','FOLD','CHECK'].includes(a.actionType) ? null : a.seatScore ?? null,
          source_round: typeof a.round === 'string' ? a.round : null,
        };
        const old = destination.get(id);
        if (old && (old.player_id !== value.player_id || old.action !== value.action || old.amount_delta !== value.amount_delta)) throw new Error('Conflicting action history');
        if (!old || type === 'WP_actionNotify') destination.set(id, value);
        else if (old.source_round === null && value.source_round !== null) destination.set(id, { ...old, source_round: value.source_round });
      }
      if (destination.size > 256) throw new Error('Observation history limit exceeded');
    }
    if (type === 'WP_userOptNotify') turn = body;
    if (type === 'WP_roundChangeNotify') turn = body?.userAciton ?? null;
    if (type === 'WP_playResultNotify' || type === 'C_cleanNotify') turn = null;
    emit();
  }
  let stopped = false;
  function safeCapture(message) {
    if (stopped) return;
    try { capture(message); }
    catch { stopped = true; window.texasResearchEmit(JSON.stringify({ error: 'Observation failed; stop and resynchronize' })); }
  }
  const wrapper = function (...args) {
    const result = original.apply(this, args);
    safeCapture(args[0]);
    return result;
  };
  client._dispatchMessageToCC = wrapper;
  const timer = setInterval(() => safeCapture(null), 1000);
  window.__texasResearchObserver = {
    stop() {
      stopped = true; clearInterval(timer);
      if (client._dispatchMessageToCC === wrapper) client._dispatchMessageToCC = original;
      delete window.__texasResearchObserver;
    },
  };
  safeCapture(null);
  return { installed: true };
}
