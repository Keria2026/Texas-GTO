import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import WS, { WebSocketServer } from 'ws';
import { parseCaptureProfiles } from '../extension/capture-profiles.ts';
import { ResearchSocket } from '../extension/transport.ts';
import { database, until } from './helpers.ts';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/capture-profiles.json', import.meta.url), 'utf8'));
test('observed profiles validate counts, scope and calibrated probabilities', () => {
  assert.deepEqual(parseCaptureProfiles(fixture), fixture);
  for (const mutate of [
    (m: any) => { m.identity_scope = 'all_players'; },
    (m: any) => { m.players[0].action_observations = 4; },
    (m: any) => { m.players[0].contexts[0].counts.FOLD = -1; },
    (m: any) => { m.players[0].contexts[0].counts.CALL = 3; },
    (m: any) => { m.players[0].contexts[0].frequencies.CALL = 1; },
    (m: any) => { m.players[0].contexts[0].posterior_probabilities.CALL = NaN; },
    (m: any) => { m.players[0].contexts[0].posterior_probabilities = m.players[0].contexts[0].frequencies; },
    (m: any) => { m.players[0].contexts[0].prior_per_action = 0; },
    (m: any) => { m.players[0].contexts.push(m.players[0].contexts[0]); },
    (m: any) => { m.players.push(m.players[0]); },
    (m: any) => { m.players[0].player_id = 'player-18446744073709551616'; },
    (m: any) => { m.through_sequence = '0'; },
    (m: any) => { m.players[0].contexts[0].observations = true; },
    (m: any) => { m.actions = ['CALL']; },
  ]) {
    const bad = structuredClone(fixture); mutate(bad);
    assert.throws(() => parseCaptureProfiles(bad));
  }
});

test('profiles are capability-, business-source- and ACK-bound and clear on disconnect', async () => {
  const db = await database(), net = await db.create(1, false), business = await db.create(1, false, net.id);
  const gap = { kind: 'capture_gap', table_id: null, hand_id: null, data: { reason: 'synthetic' } };
  await db.appendBusiness(business.id, [gap]);
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
  await new Promise<void>(r => server.once('listening', r));
  const port = (server.address() as { port: number }).port;
  globalThis.WebSocket = class extends WS {
    constructor(_url: string) { super(`ws://127.0.0.1:${port}/v1/research/stream`); }
  } as unknown as typeof WebSocket;
  let socket: WS; let state = '';
  const appends: unknown[][] = [];
  const message = { ...fixture, source_id: business.id };
  server.on('connection', ws => {
    socket = ws;
    ws.on('message', bytes => {
      const m = JSON.parse(bytes.toString());
      if (m.type === 'auth') {
        assert.ok(m.capabilities.includes('capture_profiles_v1'));
        ws.send(JSON.stringify({ type: 'ready', schema_version: '1.2', capabilities: ['capture_events_v1', 'capture_profiles_v1'] }));
      } else if (m.type === 'capture.append') {
        appends.push(m.events);
        ws.send(JSON.stringify({ type: 'capture.ack', schema: 'tgto.capture/0.1', source_id: business.id, through_sequence: m.events.at(-1).sequence }));
        ws.send(JSON.stringify({ ...message, through_sequence: m.events.at(-1).sequence }));
      }
    });
  });
  const client = new ResearchSocket(db, s => { state = s; });
  const processed = async () => {
    let pong = false; client.pong = () => { pong = true; };
    socket!.send('{"type":"pong"}'); await until(() => pong);
  };
  try {
    client.connect('ws://127.0.0.1:8766/v1/research/stream', 'synthetic-profile-token');
    await until(() => client.captureProfiles.has(business.id));
    client.stop(); assert.equal(client.captureProfiles.size, 0);
    client.connect('ws://127.0.0.1:8766/v1/research/stream', 'synthetic-profile-token');
    await until(() => client.captureProfiles.has(business.id));
    assert.equal(appends.length, 2);
    assert.deepEqual(appends[1], appends[0]);
    await client.pump(); await processed();
    assert.equal(appends.length, 2); // The idle refresh does not become an append loop.
    assert.equal(client.flight, undefined);
    assert.equal(client.captureProfiles.get(business.id)?.through_sequence, '1');
    client.captureProfiles.clear();
    await db.appendBusiness(business.id, [gap]);
    socket!.send(JSON.stringify(message)); await processed();
    assert.equal(client.captureProfiles.size, 0);
    socket!.send(JSON.stringify({ ...message, through_sequence: '3' }));
    await until(() => state === 'error');
    assert.equal(client.captureProfiles.size, 0);
    assert.equal((await db.sessions()).find(s => s.id === business.id)!.ack, '1');
  } finally {
    client.stop(); for (const ws of server.clients) ws.terminate();
    await new Promise<void>(r => server.close(() => r())); db.db.close();
  }
});

test('unnegotiated profiles and profiles for unknown or network sources reject without advancing ACK', async () => {
  for (const mode of ['unnegotiated', 'unknown', 'network']) {
    const db = await database(), net = await db.create(1, false), business = await db.create(1, false, net.id);
    const server = new WebSocketServer({ host: '127.0.0.1', port: 0 });
    await new Promise<void>(r => server.once('listening', r));
    const port = (server.address() as { port: number }).port;
    globalThis.WebSocket = class extends WS {
      constructor(_url: string) { super(`ws://127.0.0.1:${port}/v1/research/stream`); }
    } as unknown as typeof WebSocket;
    server.on('connection', ws => ws.on('message', bytes => {
      if (JSON.parse(bytes.toString()).type !== 'auth') return;
      ws.send(JSON.stringify({ type: 'ready', schema_version: '1.2', capabilities: ['capture_events_v1', ...(mode === 'unnegotiated' ? [] : ['capture_profiles_v1'])] }));
      ws.send(JSON.stringify({ ...fixture, source_id: mode === 'network' ? net.id : mode === 'unknown' ? 'missing-source' : business.id }));
    }));
    let state = ''; const client = new ResearchSocket(db, s => { state = s; });
    try {
      client.connect('ws://127.0.0.1:8766/v1/research/stream', 'synthetic-profile-token');
      await until(() => state === 'error');
      assert.equal(client.captureProfiles.size, 0);
      assert.ok((await db.sessions()).every(s => s.ack === '0'));
    } finally {
      client.stop(); for (const ws of server.clients) ws.terminate();
      await new Promise<void>(r => server.close(() => r())); db.db.close();
    }
  }
});
