import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import WS, { WebSocketServer } from 'ws';
import { database, until } from './helpers.ts';
import { ResearchSocket } from '../extension/transport.ts';
import { startBackend } from '../tools/reference-backend.ts';
import { SCHEMA } from '../extension/model.ts';

const origin = `chrome-extension://${'a'.repeat(32)}`;
const token = 'synthetic-test-token-not-an-account-credential';
const url = 'ws://127.0.0.1:8766/v1/research/stream';
class TestSocket extends WS { constructor(url: string) { super(url, { origin }); } }
globalThis.WebSocket = TestSocket as unknown as typeof WebSocket;

test('an alarm reconnect releases a closed socket and does not duplicate an open connection', async () => {
  const db = await database();
  const server = await startBackend({ port: 8766, database: ':memory:', token, origin });
  let state = ''; const client = new ResearchSocket(db, value => { state = value; });
  try {
    client.connect(url, token); await until(() => state === 'ready');
    const first = client.socket; client.open(); client.open();
    assert.equal(client.socket, first);
    for (const socket of server.wss.clients) socket.terminate();
    await until(() => state === 'retrying');
    assert.equal(client.socket, undefined);
    client.open(); const reconnecting = client.socket; client.open();
    assert.equal(client.socket, reconnecting);
    await until(() => state === 'ready');
    assert.equal(server.wss.clients.size, 1);
  } finally { client.stop(); await server.close(); db.db.close(); }
});

test('real WS auth, dropped ACK, replay and durable backend restart', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tgto-backend-'));
  const options = { port: 8766, database: join(directory, 'capture.sqlite'), token, origin };
  const db = await database(); const session = await db.create(1, true);
  await db.append(session.id, 'capture_gap', { reason: 'synthetic-start' }, { encoding: 'utf8', value: 'local-only-sensitive-fixture' });
  const statuses: string[] = []; const client = new ResearchSocket(db, state => statuses.push(state));
  let server = await startBackend(options);
  let drop = true;
  server.wss.on('connection', socket => {
    const original = socket.send;
    Object.defineProperty(socket, 'send', { value(data: unknown, ...args: unknown[]) {
      if (drop && String(data).includes('network.ack')) { drop = false; socket.terminate(); return; }
      return Reflect.apply(original, socket, [data, ...args]);
    } });
  });
  try {
    client.connect(url, token);
    await until(async () => (await db.sessions())[0].ack === '1');
    assert.ok(statuses.includes('retrying'));
    assert.equal((server.db.prepare('SELECT COUNT(*) AS n FROM events').get() as { n: number }).n, 1);
    assert.ok(!String((server.db.prepare('SELECT body FROM events').get() as { body: string }).body).includes('local-only'));
    await server.close();
    await until(() => statuses.at(-1) === 'retrying');
    await db.append(session.id, 'capture_gap', { reason: 'offline-fixture' }); client.notify();
    assert.equal((await db.sessions())[0].ack, '1');
    server = await startBackend(options);
    await until(async () => (await db.sessions())[0].ack === '2');
    assert.equal((server.db.prepare('SELECT COUNT(*) AS n FROM events').get() as { n: number }).n, 2);
  } finally { client.stop(); await server.close(); db.db.close(); await rm(directory, { recursive: true, force: true }); }
});

test('future ACK, wrong source and unsupported capability never clear the queue', async () => {
  for (const mode of ['future', 'wrong-source', 'unsupported']) {
    const db = await database(); const session = await db.create(1, false);
    await db.append(session.id, 'capture_gap', { reason: 'synthetic' });
    const wss = new WebSocketServer({ host: '127.0.0.1', port: 8766 });
    await new Promise<void>(resolve => wss.once('listening', resolve));
    wss.on('connection', ws => ws.on('message', bytes => {
      const m = JSON.parse(bytes.toString());
      if (m.type === 'auth') ws.send(JSON.stringify({ type: 'ready', schema_version: '1.2', capabilities: mode === 'unsupported' ? [] : ['network_events_v1'] }));
      if (m.type === 'network.append') ws.send(JSON.stringify({ type: 'network.ack', schema: SCHEMA,
        source_id: mode === 'wrong-source' ? 'wrong' : session.id, through_sequence: mode === 'future' ? '999' : '1' }));
    }));
    let state = ''; const client = new ResearchSocket(db, value => { state = value; });
    try {
      client.connect(url, token); await until(() => state === 'error' || state === 'unsupported');
      assert.equal((await db.sessions())[0].ack, '0'); assert.equal((await db.batch())?.events.length, 1);
    } finally { client.stop(); for (const ws of wss.clients) ws.terminate(); await new Promise<void>(resolve => wss.close(() => resolve())); db.db.close(); }
  }
});

test('reference receiver rejects missing Origin, bad token and conflicting event content', async () => {
  const server = await startBackend({ port: 8766, database: ':memory:', token, origin });
  async function exchange(messages: object[], expected: string) {
    const ws = new TestSocket(url); const received: Record<string, unknown>[] = [];
    ws.on('message', data => { const m = JSON.parse(data.toString()); received.push(m);
      if (m.type === 'ready') messages.forEach(value => ws.send(JSON.stringify(value)));
    });
    await new Promise<void>(resolve => ws.once('open', resolve)); ws.send(JSON.stringify({ type: 'auth', token }));
    await until(() => received.some(m => m.type === expected)); ws.terminate(); return received;
  }
  try {
    const event = { schema: SCHEMA, type: 'capture_gap', source_id: 'fixture-source', sequence: '1', captured_at: '2026-11-01T00:00:00Z', data: { reason: 'initial' } };
    const batch = { schema: SCHEMA, type: 'network.append', source_id: 'fixture-source', events: [event] };
    await exchange([batch], 'network.ack');
    await exchange([batch], 'network.ack');
    await exchange([{ ...batch, events: [{ ...event, data: { reason: 'conflict' } }] }], 'error');
    assert.equal((server.db.prepare('SELECT COUNT(*) AS n FROM events').get() as { n: number }).n, 1);
    const noOrigin = new WS(url);
    await new Promise<void>(resolve => { noOrigin.once('error', () => resolve()); });
    const bad = new TestSocket(url); let rejected = false;
    bad.on('message', b => { rejected = JSON.parse(b.toString()).type === 'error'; });
    await new Promise<void>(resolve => bad.once('open', resolve)); bad.send('{"type":"auth","token":"wrong-synthetic-token"}');
    await until(() => rejected); bad.terminate();
  } finally { await server.close(); }
});

test('negotiated server status rejects future results and clears on disconnect', async () => {
  const db = await database(); const session = await db.create(1, false);
  await db.append(session.id, 'capture_gap', { reason: 'synthetic' });
  const wss = new WebSocketServer({ host: '127.0.0.1', port: 8766 });
  await new Promise<void>(resolve => wss.once('listening', resolve));
  let connection: WS;
  const status = { type: 'network.status', schema: 'tgto.network-status/0.1', source_id: session.id,
    through_sequence: '1', status: 'blocked', reason: 'business_events_unavailable', actions: [], missing_fields: ['hero_cards'] };
  wss.on('connection', ws => {
    connection = ws;
    ws.on('message', bytes => {
      const m = JSON.parse(bytes.toString());
      if (m.type === 'auth') {
        assert.deepEqual(m.capabilities, ['network_status_v1', 'capture_state_v1', 'capture_profiles_v1', 'capture_recommendations_v1', 'capture_search_v1', 'capture_pr_v1']);
        ws.send(JSON.stringify({ type: 'ready', schema_version: '1.2', capabilities: ['network_events_v1', 'network_status_v1'] }));
      }
      if (m.type === 'network.append') {
        ws.send(JSON.stringify({ type: 'network.ack', schema: SCHEMA, source_id: session.id, through_sequence: m.events.at(-1).sequence }));
        ws.send(JSON.stringify(status));
      }
    });
  });
  let state = ''; const client = new ResearchSocket(db, value => { state = value; });
  try {
    client.connect(url, token); await until(() => !!client.serverStatus);
    assert.equal(client.serverStatus?.through_sequence, '1');
    // Stale results must not replace the current status after another event is captured.
    client.serverStatus = undefined;
    await db.append(session.id, 'capture_gap', { reason: 'newer' }); client.notify();
    await until(async () => (await db.sessions())[0].ack === '2');
    assert.equal(client.serverStatus, undefined);
    connection!.send(JSON.stringify({ ...status, through_sequence: '3' }));
    await until(() => state === 'error');
    assert.equal(client.serverStatus, undefined);
  } finally { client.stop(); for (const ws of wss.clients) ws.terminate(); await new Promise<void>(resolve => wss.close(() => resolve())); db.db.close(); }
});

test('network and business streams are independently acknowledged and scheduled fairly', async () => {
  const db = await database(); const net = await db.create(1, false);
  for (let i = 0; i < 80; i++) await db.append(net.id, 'capture_gap', { reason: 'synthetic-network' });
  const business = await db.create(1, false, net.id);
  await db.appendBusiness(business.id, [{ kind: 'capture_gap', table_id: null, hand_id: null, data: { reason: 'synthetic-business' } }]);
  const server = await startBackend({ port: 8766, database: ':memory:', token, origin });
  const order: string[] = [];
  server.wss.on('connection', ws => ws.on('message', bytes => { const m = JSON.parse(bytes.toString()); if (m.type.endsWith('.append')) order.push(m.type); }));
  const client = new ResearchSocket(db, () => {});
  try {
    client.connect(url, token);
    await until(async () => (await db.sessions()).every(s => s.last === s.ack));
    assert.deepEqual(order, ['network.append', 'capture.append', 'network.append']);
    assert.equal((server.db.prepare('SELECT COUNT(*) AS n FROM events').get() as { n: number }).n, 81);
  } finally { client.stop(); await server.close(); db.db.close(); }
});

test('network-only backend keeps business events pending without blocking network ACKs', async () => {
  const db = await database(); const net = await db.create(1, false), business = await db.create(1, false, net.id);
  await db.append(net.id, 'capture_gap', { reason: 'network' });
  await db.appendBusiness(business.id, [{ kind: 'capture_gap', table_id: null, hand_id: null, data: { reason: 'business' } }]);
  const wss = new WebSocketServer({ port: 8766, host: '127.0.0.1' }); await new Promise<void>(r => wss.once('listening', r));
  let businessSent = false;
  wss.on('connection', ws => ws.on('message', bytes => {
    const m = JSON.parse(bytes.toString());
    if (m.type === 'auth') ws.send(JSON.stringify({ type: 'ready', schema_version: '1.2', capabilities: ['network_events_v1'] }));
    if (m.type === 'capture.append') businessSent = true;
    if (m.type === 'network.append') ws.send(JSON.stringify({ type: 'network.ack', schema: SCHEMA, source_id: m.source_id, through_sequence: m.events.at(-1).sequence }));
  }));
  const client = new ResearchSocket(db, () => {});
  try { client.connect(url, token); await until(async () => (await db.sessions()).find(s => s.id === net.id)!.ack === '1');
    assert.equal((await db.sessions()).find(s => s.id === business.id)!.ack, '0'); assert.equal(businessSent, false);
  } finally { client.stop(); for (const ws of wss.clients) ws.terminate(); await new Promise<void>(r => wss.close(() => r())); db.db.close(); }
});
