// All site-private reads live here. DTOs never include raw identities, credentials or opponent cards.
declare const HMFClient: any;
declare const __require: (name: string) => any;
declare const CurrentUserInfo: any;

export function installWpkPage(binding: string) {
  const root = globalThis as any;
  root.__tgtoWpkBridge?.stop();
  let original: any, client: any, wrapper: any, stopped = false, initialOwn = '', previous = '', lastVisible: any;
  const aliases = new Map<string, string>(), tables = new Map<string, string>();
  let phase = '', roomClosed = false;
  const report = (status: string) => { if (phase !== status) { phase = status; root[binding](JSON.stringify({ status })); } };
  const identity = (value: any) => {
    if (typeof value === 'number' && !Number.isSafeInteger(value)) throw Error('Unsafe identity');
    const id = String(value); if (!/^[0-9]{1,20}$/.test(id) || id === '0') throw Error('Invalid identity'); return id;
  };
  const player = (value: any) => {
    const id = identity(value); if (id === initialOwn) return 'hero';
    if (!aliases.has(id)) { if (aliases.size >= 256) throw Error('Alias limit'); aliases.set(id, `player-${aliases.size + 1}`); }
    return aliases.get(id)!;
  };
  const table = (value: any) => {
    const id = identity(value);
    if (!tables.has(id)) { if (tables.size >= 16) throw Error('Table limit'); tables.set(id, `table-${tables.size + 1}`); }
    return tables.get(id)!;
  };
  const integer = (value: any) => Number.isSafeInteger(value) ? value : null;
  const list = (value: any, maximum: number) => {
    if (value === undefined || value === null) return [];
    if (!Array.isArray(value) || value.length > maximum) throw Error('Invalid bounded list'); return value;
  };
  const cards = (value: any, own = false) => {
    if (typeof value === 'string' && value.startsWith('[') && value.length <= 512) value = JSON.parse(value);
    if (own && !Array.isArray(value)) return [null, null];
    return list(value, own ? 2 : 5).map((v: any) => integer(v));
  };
  const turn = (value: any) => value?.userId ? {
    player_id: player(value.userId),
    legal_actions: player(value.userId) === 'hero' ? list(value.canActionList, 6).map(String) : [],
    call_amount: player(value.userId) === 'hero' ? integer(value.callScore) : null,
    min_raise_reported: player(value.userId) === 'hero' ? integer(value.minRaiseScore) : null,
    max_raise_reported: player(value.userId) === 'hero' ? integer(value.maxRaiseScore) : null,
  } : null;
  const resultPlayers = (value: any, log: boolean) => list(value, 8).map((p: any) => ({
    player_id: player(p.userId), net_chips: integer(log ? p.changeScore : p.addScore),
    mushroom_paid: integer(p.mushroomPayScore), mushroom_awarded: integer(p.mushroomScore),
  }));
  function publish(message: any) {
    const name = message?.msgType ?? 'snapshot';
    const selected = ['snapshot', 'WP_bankerChangeNotify', 'WP_dealNotify', 'WP_actionNotify', 'WP_actionHistoryNotify', 'WP_userOptNotify', 'WP_roundChangeNotify', 'WP_playResultNotify', 'C_playLogNotify', 'C_cleanNotify', 'C_closeRoomNotify'];
    if (!selected.includes(name)) return;
    if (message?.errorCode !== undefined && message.errorCode !== 0) throw Error('Failed source message');
    if (['WP_dealNotify', 'WP_actionNotify', 'WP_actionHistoryNotify', 'WP_userOptNotify', 'WP_roundChangeNotify', 'WP_playResultNotify', 'C_playLogNotify'].includes(name)
      && (!message.msgBody || typeof message.msgBody !== 'object' || Array.isArray(message.msgBody))) throw Error('Invalid source body');
    if (name === 'snapshot' && roomClosed) return;
    if (name === 'WP_dealNotify' || name === 'WP_bankerChangeNotify') roomClosed = false;
    const game = __require('WPGameData').WPGameData.Instance;
    if (!CurrentUserInfo?.user?.userId || String(CurrentUserInfo.user.userId) === '0') {
      if (lastVisible) throw Error('Own identity lost'); report('waiting_seat'); return;
    }
    const currentOwn = identity(CurrentUserInfo.user.userId);
    if (initialOwn && initialOwn !== currentOwn) throw Error('Account changed');
    const people = list(game.sitUserList, 8).filter((p: any) => p.userId && p.seatNum >= 1 && p.seatNum <= 8);
    const mine = people.find((p: any) => identity(p.userId) === currentOwn);
    const body = message?.msgBody ?? {};
    let visible;
    if (mine && integer(game.currentBoutNum) && game.currentBoutNum > 0) {
      initialOwn ||= currentOwn;
      visible = { table_id: table(CurrentUserInfo.roomId), hand_number: integer(game.currentBoutNum), round: String(game.round ?? ''),
        hero_cards: cards(mine.handCards, true), board: cards(game.publicCards),
        players: people.map((p: any) => ({ player_id: player(p.userId), seat: integer(p.seatNum), stack: integer(p.currentScore) })) };
      lastVisible = visible;
    } else if (lastVisible && ['WP_playResultNotify', 'C_playLogNotify', 'C_closeRoomNotify'].includes(name)) visible = lastVisible;
    else { if (lastVisible) throw Error('Own seat no longer visible'); report('waiting_seat'); return; }
    const data: any = { version: 'wpk/1', event: name, visible, deal_hand: name === 'WP_dealNotify' ? integer(body.currentBoutNum) : null,
      actions: [], turn: null, result: null, log: null };
    if (['WP_actionNotify', 'WP_actionHistoryNotify'].includes(name)) data.actions = list(body.actionList, 256).map((a: any) => ({
      source_event_id: identity(a.actionId), player_id: player(a.userId), action: String(a.actionType),
      amount_delta: integer(a.actionScore), amount_to: integer(a.seatScore), source_round: typeof a.round === 'string' ? a.round : null,
    }));
    if (name === 'WP_userOptNotify') data.turn = turn(body);
    if (name === 'WP_roundChangeNotify') data.turn = turn(body.userAciton); // Actual source spelling.
    if (name === 'WP_playResultNotify') data.result = { table_id: visible.table_id, hand_number: visible.hand_number,
      players: resultPlayers(body.thanList, false), double_board: Boolean(body.isDealDouble) };
    if (name === 'C_playLogNotify') {
      const rows = list(body.handList1, 8); if (!rows.length) throw Error('Missing final hand log');
      const room = table(rows[0].roomId); if (rows.some((p: any) => table(p.roomId) !== room)) throw Error('Mixed settlement rooms');
      const own = rows.find((p: any) => identity(p.userId) === initialOwn);
      data.log = { table_id: room, hand_number: integer(body.handNum), players: resultPlayers(rows, true),
        board: cards(rows[0].publicCardAll), hero_cards: own ? cards(own.handCard, true) : [null, null], double_board: list(body.handList2, 8).length > 0 };
    }
    const text = JSON.stringify(data);
    if (text.length > 256 * 1024) throw Error('DTO too large');
    if (name === 'snapshot' && text === previous) return;
    previous = text; report('ready'); root[binding](JSON.stringify({ dto: data }));
    if (name === 'C_closeRoomNotify') roomClosed = true;
  }
  function stop() {
    if (stopped) return; stopped = true; clearInterval(timer);
    if (client && client._dispatchMessageToCC === wrapper) client._dispatchMessageToCC = original;
    if (root.__tgtoWpkBridge?.binding === binding) delete root.__tgtoWpkBridge;
  }
  function tick(message?: any) {
    if (stopped) return;
    try {
      if (!client) {
        if (typeof HMFClient === 'undefined' || typeof __require !== 'function' || typeof CurrentUserInfo === 'undefined') { report('waiting_client'); return; }
        const instance = HMFClient.sharedInstance();
        let gameReady = false;
        try { gameReady = !!__require('WPGameData')?.WPGameData?.Instance; } catch { /* Lazy-loaded game module. */ }
        if (typeof instance?._dispatchMessageToCC !== 'function' || !gameReady) { report('waiting_client'); return; }
        client = instance; original = client._dispatchMessageToCC;
        wrapper = function(this: any, ...args: any[]) { const result = Reflect.apply(original, this, args); tick(args[0]); return result; };
        client._dispatchMessageToCC = wrapper;
      }
      publish(message);
    } catch { try { report('adapter_error'); } catch { /* A removed binding cannot affect the client. */ } finally { stop(); } }
  }
  const timer = setInterval(() => tick(), 500);
  root.__tgtoWpkBridge = { binding, stop }; tick();
  return { installed: true };
}
