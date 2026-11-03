// Synthetic WPK page → production WASM → IndexedDB → real Python WebSocket/SQLite.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import WS from 'ws';
import { database, until } from './helpers.ts';
import { fixture, normalize } from './wpk-fixture.ts';
import { ResearchSocket } from '../extension/transport.ts';
import { currentRecommendation } from '../extension/recommendation.ts';

const root = resolve(process.env.TEXAS_GTO_ROOT ?? '..');
const python = join(root, '.venv/bin/python');
const origin = `chrome-extension://${'a'.repeat(32)}`;
const token = 'synthetic-cross-repository-test-token-not-a-credential';
const url = 'ws://127.0.0.1:8766/v1/research/stream';
let backendPort;
let dropAck = true;
class PythonSocket extends WS {
  constructor(_url) { super(`ws://127.0.0.1:${backendPort}/v1/research/stream`, { origin }); }
  emit(event, ...args) {
    if (event === 'message' && dropAck && JSON.parse(args[0].toString()).type === 'capture.ack') {
      dropAck = false; this.terminate();
      super.emit('message', Buffer.from('{"type":"error","code":"stale-buffered-frame"}'), false);
      return false;
    }
    return super.emit(event, ...args);
  }
}
globalThis.WebSocket = PythonSocket;

test('eight-player observations → Python pinned policy → extension recommendation, then stale invalidation', { timeout: 40000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tgto-eight-integration-'));
  const db = await database();
  let server, diagnostics = '';
  const client = new ResearchSocket(db, () => {});
  try {
    const events = JSON.parse(execFileSync(python, ['-c', `
import json,sys
from pathlib import Path
sys.path.insert(0, 'services/analysis/tests')
from test_capture_recommendation import eight_player_events,fixture_model
events=eight_player_events()
path=Path(sys.argv[1])/'core'/'training.sqlite3'
path.parent.mkdir()
fixture_model(path, events)
print(json.dumps(events))
`, directory], { cwd: root, encoding: 'utf8' }));
    server = spawn(python, ['-m', 'texas_analysis'], { cwd: root,
      env: { ...process.env, TEXAS_PORT: '0', TEXAS_AUTO_TRAIN: '0', TEXAS_ALLOWED_ORIGINS: origin,
        TEXAS_API_TOKEN: token, TEXAS_DATA_DIR: directory }, stdio: ['ignore', 'pipe', 'pipe'] });
    server.stderr.on('data', bytes => { diagnostics += bytes.toString(); });
    await until(() => {
      if (server.exitCode !== null) throw Error(`Python startup failed: ${diagnostics}`);
      const match = diagnostics.match(/Uvicorn running on http:\/\/127\.0\.0\.1:(\d+)/);
      if (!match) return false; backendPort = Number(match[1]); return true;
    });
    const net = await db.create(1, false), business = await db.create(1, false, net.id);
    await db.appendBusiness(business.id, events.map(({ kind, table_id, hand_id, data }) => ({ kind, table_id, hand_id, data })));
    dropAck = false; client.connect(url, token);
    await until(() => client.recommendations.get(business.id)?.status === 'ready');
    const recommendation = client.recommendations.get(business.id);
    const source = (await db.sessions()).find(s => s.id === business.id);
    assert.equal(currentRecommendation(recommendation, source, client.captureStates.get(business.id)), recommendation);
    assert.equal(recommendation.model.source, 'pluribus_blueprint');
    assert.ok(recommendation.actions.some(a => a.action === 'CALL' && a.amount_delta === 8));
    await db.appendBusiness(business.id, [{ kind: 'capture_gap', table_id: 'table-1', hand_id: 'table-1:2', data: { reason: 'synthetic interrupted decision' } }]);
    const changed = (await db.sessions()).find(s => s.id === business.id);
    assert.equal(currentRecommendation(recommendation, changed, client.captureStates.get(business.id)), undefined);
    client.notify(); await until(() => client.recommendations.get(business.id)?.status === 'unavailable');
    assert.deepEqual(client.recommendations.get(business.id).actions, []);
    client.stop(); assert.equal(client.recommendations.size, 0);
  } finally {
    client.stop(); db.db.close();
    if (server && server.exitCode === null) { const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited; }
    await rm(directory, { recursive: true, force: true });
    dropAck = true;
  }
});

test('WPK observer → WASM → Python: business ACK loss/replay, durable projection and late settlement', { timeout: 40000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tgto-wpk-integration-'));
  const db = await database();
  let server, diagnostics = '';
  async function start() {
    diagnostics = '';
    server = spawn(python, ['-m', 'texas_analysis'], { cwd: root,
      env: { ...process.env, TEXAS_PORT: '0', TEXAS_ALLOWED_ORIGINS: origin, TEXAS_API_TOKEN: token, TEXAS_DATA_DIR: directory },
      stdio: ['ignore', 'pipe', 'pipe'] });
    server.stdout.on('data', b => { diagnostics += b.toString(); });
    server.stderr.on('data', b => { diagnostics += b.toString(); });
    await until(async () => {
      if (server.exitCode !== null) throw Error(`Python startup failed: ${diagnostics}`);
      const listening = diagnostics.match(/Uvicorn running on http:\/\/127\.0\.0\.1:(\d+)/);
      if (!listening) return false;
      backendPort = Number(listening[1]);
      return (await fetch(`http://127.0.0.1:${backendPort}/v1/health`)).ok;
    });
  }
  async function stop() {
    if (!server || server.exitCode !== null || server.signalCode !== null) return;
    const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited;
  }
  const states = [], client = new ResearchSocket(db, (state, error) => states.push([state, error]));
  try {
    const f = fixture(); f.fixture.play(); const normalized = normalize(f.messages);
    // Keep the standalone Python golden fixture tied to actual frontend production output.
    const golden = JSON.parse(await readFile(join(root, 'fixtures/hands/wpk-observed-synthetic.json'), 'utf8'));
    assert.deepEqual(golden.events.map(({ kind, table_id, hand_id, data }) => ({ kind, table_id, hand_id, data })), normalized.events);
    const net = await db.create(7, false), business = await db.create(7, false, net.id);
    await db.append(net.id, 'capture_gap', { reason: 'synthetic-network' });
    const gap = { kind: 'capture_gap', table_id: null, hand_id: null, data: { reason: 'synthetic' } };
    await db.appendBusiness(business.id, Array.from({ length: 70 }, () => gap));
    await db.appendBusiness(business.id, normalized.events, normalized.checkpoint);
    const last = String(70 + normalized.events.length);
    await start(); client.connect(url, token);
    await until(async () => (await db.sessions()).find(s => s.id === business.id).ack === last);
    await until(() => client.captureStates.get(business.id)?.through_sequence === last);
    await until(() => client.captureProfiles.get(business.id)?.through_sequence === last);
    assert.equal(dropAck, false);
    const state = client.captureStates.get(business.id);
    assert.equal(state.state.hand_id, 'table-1:3');
    assert.equal(state.state.closed, true);
    assert.equal(state.state.settlements[0].hand_id, 'table-1:2');
    for (const key of ['street', 'players', 'hero_cards', 'board', 'actions', 'history_complete', 'turn'])
      assert.deepEqual(state.state[key], normalized.checkpoint[key], key);
    assert.deepEqual(state.analysis.actions, []);
    assert.equal(state.analysis.status, 'unsupported');
    const profiles = client.captureProfiles.get(business.id);
    assert.equal(profiles.identity_scope, 'origin_source');
    assert.ok(profiles.players.some(p => p.action_observations > 0));
    assert.deepEqual(profiles.players.map(p => p.player_id), state.state.players.map(p => p.player_id));
    client.stop(); assert.equal(client.captureStates.size, 0); assert.equal(client.captureProfiles.size, 0); await stop();
    await start();
    // No new events or outbox entries: reconnect restores both projections by replaying
    // one already acknowledged event, without increasing the learned observation count.
    assert.equal((await db.sessions()).find(s => s.id === business.id).pendingBytes, 0);
    client.connect(url, token);
    await until(() => client.captureStates.get(business.id)?.through_sequence === last);
    await until(() => client.captureProfiles.get(business.id)?.through_sequence === last);
    assert.deepEqual(client.captureStates.get(business.id), state);
    assert.deepEqual(client.captureProfiles.get(business.id), profiles);
    assert.equal(client.flight, undefined);
    client.stop();
    // Explicit replay from retained archive; no local raw bytes are part of this stream.
    await db.prepareBackend('synthetic-other-receiver'); client.connect(url, token);
    await until(() => client.captureStates.get(business.id)?.through_sequence === last);
    await until(() => client.captureProfiles.get(business.id)?.through_sequence === last);
    assert.deepEqual(client.captureProfiles.get(business.id), profiles);
    client.stop(); await stop();
    const stored = JSON.parse(execFileSync(python, ['-c', `import sqlite3,json,sys
with sqlite3.connect(sys.argv[1]) as db:
 print(json.dumps({'count':db.execute('SELECT COUNT(*) FROM capture_events').fetchone()[0], 'body':db.execute('SELECT body FROM capture_state').fetchone()[0]}))`, join(directory, 'capture.sqlite3')], { encoding: 'utf8' }));
    assert.equal(stored.count, Number(last));
    assert.equal(JSON.parse(stored.body).hand_id, 'table-1:3');
    for (const secret of ['TOP_SECRET_FORBIDDEN', 'PRIVATE_NICKNAME', 'DO_NOT_READ', '912345', '967890']) assert.ok(!stored.body.includes(secret));
    assert.ok(!states.some(([s]) => s === 'error'), JSON.stringify(states));
  } finally { client.stop(); await stop(); db.db.close(); await rm(directory, { recursive: true, force: true }); }
});
