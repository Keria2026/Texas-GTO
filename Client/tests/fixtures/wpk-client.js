// SYNTHETIC client with the field shapes documented by the historical WPK prototype.
(() => {
  const own = 912345, second = 967890, third = 987654;
  const people = [own, second, third].map((userId, i) => ({ userId, seatNum: i + 1, currentScore: 800,
    ...(i === 0 ? { handCards: [205, 210] } : {}), nickname: 'PRIVATE_NICKNAME' }));
  for (const p of people.slice(1)) Object.defineProperty(p, 'handCards', { get() { throw Error('Opponent cards must never be read'); } });
  const game = { currentBoutNum: 1, round: 'PRE_FLOP', publicCards: [], sitUserList: people };
  const failure = Error('original-client-error'); let calls = 0;
  const original = function(message) {
    calls++;
    if (message.msgType === 'SYNTHETIC_THROW') throw failure;
    const body = message.msgBody ?? {};
    if (message.msgType === 'WP_dealNotify') { game.currentBoutNum = body.currentBoutNum; game.round = 'PRE_FLOP'; game.publicCards = []; people[0].handCards = body.currentBoutNum === 3 ? [101, 413] : [205, 210]; }
    if (message.msgType === 'WP_roundChangeNotify') { game.round = body.round;
      if (body.dealPublicCards) game.publicCards = body.round === 'FLOP' ? body.dealPublicCards : game.publicCards.concat(body.dealPublicCards); }
    return 17;
  };
  const client = { _dispatchMessageToCC: original };
  globalThis.HMFClient = { sharedInstance: () => client };
  globalThis.CurrentUserInfo = { user: { userId: own }, roomId: 223344, sessionToken: 'TOP_SECRET_FORBIDDEN' };
  globalThis.__require = name => { if (name !== 'WPGameData') throw Error('Unexpected module'); return { WPGameData: { Instance: game } }; };
  const emit = (msgType, msgBody = {}) => client._dispatchMessageToCC({ msgType, msgBody });
  function play() {
    emit('WP_bankerChangeNotify');
    const a = (actionId, userId, actionType, actionScore, seatScore) => ({ actionId, userId, actionType, actionScore, seatScore, round: game.round, sessionToken: 'TOP_SECRET_FORBIDDEN' });
    emit('WP_actionNotify', { actionList: [a(2001, own, 'PAY_MUSHROOM', 20, 0), a(2002, second, 'PAY_MUSHROOM', 20, 0), a(2003, third, 'PAY_MUSHROOM', 20, 0)] });
    emit('WP_dealNotify', { currentBoutNum: 2 });
    emit('WP_userOptNotify', { userId: own, canActionList: ['FOLD', 'CALL', 'RAISE'], callScore: 2, minRaiseScore: 8, maxRaiseScore: 780 });
    emit('WP_actionNotify', { actionList: [a(2004, own, 'SMALL_BLIND', 2, 2), a(2005, second, 'BIG_BLIND', 4, 4), a(2006, third, 'CALL', 4, 4), a(2007, own, 'RAISE', 10, 12), a(2008, second, 'CALL', 8, 12), a(2009, third, 'FOLD', 0, 0)] });
    emit('WP_roundChangeNotify', { round: 'FLOP', dealPublicCards: [107, 209, 403] });
    emit('WP_actionNotify', { actionList: [a(2010, own, 'CHECK', 0, 0), a(2011, second, 'CHECK', 0, 0)] });
    emit('WP_roundChangeNotify', { round: 'TURN', dealPublicCards: [111] });
    emit('WP_actionNotify', { actionList: [a(2012, own, 'CHECK', 0, 0), a(2013, second, 'CHECK', 0, 0)] });
    emit('WP_roundChangeNotify', { round: 'RIVER', dealPublicCards: [412] });
    emit('WP_actionNotify', { actionList: [a(2014, own, 'BET', 20, 20), a(2015, second, 'CALL', 20, 20)] });
    emit('WP_roundChangeNotify', { round: 'THAN', userAciton: { userId: own, canActionList: ['RAISE'], callScore: 0 } });
    const result = [own, second, third].map((userId, i) => ({ userId, addScore: [16, -52, -24][i], mushroomPayScore: 20, mushroomScore: 0, highHandCards: 'DO_NOT_READ' }));
    emit('WP_playResultNotify', { thanList: result, isDealDouble: false });
    // The previous final log may arrive after the next hand has already started.
    emit('WP_bankerChangeNotify'); emit('WP_dealNotify', { currentBoutNum: 3 });
    const log = [own, second, third].map((userId, i) => ({ userId, roomId: 223344, changeScore: [16, -52, -24][i], mushroomPayScore: 20,
      publicCardAll: '[107,209,403,111,412]', handCard: i === 0 ? '[205,210]' : 'DO_NOT_READ', nickname: 'PRIVATE_NICKNAME' }));
    emit('C_playLogNotify', { handNum: 2, handList1: log, handList2: [] });
    emit('C_closeRoomNotify');
  }
  globalThis.__wpkFixture = { game, emit, play, client, original, failure, calls: () => calls, own };
})();
