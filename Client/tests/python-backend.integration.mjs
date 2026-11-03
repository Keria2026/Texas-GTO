// Run explicitly with TEXAS_GTO_ROOT pointing to a checkout with uv sync --frozen completed.
// Uses the production ResearchSocket + actual WASM + fake IndexedDB against real Uvicorn.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import WS from 'ws';
import { database, until } from './helpers.ts';
import { ResearchSocket } from '../extension/transport.ts';

const root = resolve(process.env.TEXAS_GTO_ROOT ?? '..');
const python = join(root, '.venv/bin/python');
const origin = `chrome-extension://${'a'.repeat(32)}`;
const token = 'synthetic-cross-repository-test-token-not-a-credential';
const url = 'ws://127.0.0.1:8766/v1/research/stream';
let dropAck = true;
class PythonSocket extends WS {
  constructor(url) { super(url, { origin }); }
  emit(event, ...args) {
    if (event === 'message' && dropAck && JSON.parse(args[0].toString()).type === 'network.ack') {
      dropAck = false; this.terminate();
      // A buffered message after the connection starts closing must not halt retry.
      super.emit('message', Buffer.from('{"type":"error","code":"stale-buffered-frame"}'), false);
      return false;
    }
    return super.emit(event, ...args);
  }
}
globalThis.WebSocket = PythonSocket;

test('TGTO client → Python: lost ACK, multi-batch replay, durable restart, versioned status', { timeout: 40000 }, async () => {
  await access(python);
  const directory = await mkdtemp(join(tmpdir(), 'tgto-python-integration-'));
  const db = await database();
  let server;
  let diagnostics = '';
  async function start() {
    diagnostics = '';
    server = spawn(python, ['-m', 'texas_analysis'], { cwd: root,
      env: { ...process.env, TEXAS_PORT: '8766', TEXAS_ALLOWED_ORIGINS: origin,
        TEXAS_API_TOKEN: token, TEXAS_DATA_DIR: directory }, stdio: ['ignore', 'pipe', 'pipe'] });
    server.stdout.on('data', b => { diagnostics += b.toString(); });
    server.stderr.on('data', b => { diagnostics += b.toString(); });
    await until(async () => {
      if (server.exitCode !== null) throw Error(`Python startup failed: ${diagnostics}`);
      // Only proceed after OUR process bound its socket; an unrelated service is not a success.
      if (!diagnostics.includes('Uvicorn running on')) return false;
      return (await fetch('http://127.0.0.1:8766/v1/health')).ok;
    });
  }
  async function stop() {
    if (!server || server.exitCode !== null) return;
    const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited;
  }
  const states = [];
  const client = new ResearchSocket(db, (state, error) => states.push([state, error]));
  try {
    const session = await db.create(7, true);
    await db.append(session.id, 'capture_gap', { reason: 'synthetic-start' });
    await db.append(session.id, 'socket_open', { connection_id: 'synthetic-socket', endpoint: 'wss://example.org/game' });
    for (let n = 0; n < 68; n++) await db.append(session.id, 'network_message', {
      connection_id: 'synthetic-socket', endpoint: 'wss://example.org/game', direction: n % 2 ? 'sent' : 'received',
      opcode: n % 2 ? 1 : 2, byte_length: 18, sha256: 'a'.repeat(64), monotonic_time: n + 0.125,
    }, { encoding: 'utf8', value: 'LOCAL_ONLY_PAYLOAD' });
    await start(); client.connect(url, token);
    await until(async () => (await db.sessions())[0].ack === '70');
    await until(() => client.serverStatus?.through_sequence === '70');
    assert.ok(states.some(([s]) => s === 'retrying'), JSON.stringify(states));
    assert.deepEqual(client.serverStatus.actions, []);
    assert.equal(client.serverStatus.reason, 'business_events_unavailable');
    await stop();
    await until(() => states.at(-1)[0] === 'retrying');
    assert.equal(client.serverStatus, undefined);
    await db.append(session.id, 'capture_gap', { reason: 'synthetic-offline' }); client.notify();
    assert.equal((await db.sessions())[0].ack, '70');
    await start();
    await until(async () => (await db.sessions())[0].ack === '71');
    await until(() => client.serverStatus?.through_sequence === '71');
    const saved = JSON.parse(execFileSync(python, ['-c',
      'import json,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(json.dumps([r[0] for r in c.execute("SELECT body FROM network_events")]))',
      join(directory, 'capture.sqlite3')], { encoding: 'utf8' }));
    assert.equal(saved.length, 71);
    assert.ok(!JSON.stringify(saved).includes('LOCAL_ONLY_PAYLOAD'));
    assert.equal(new Set(saved.map(body => JSON.parse(body).sequence)).size, 71);
    assert.ok(!states.some(([s]) => s === 'error'), JSON.stringify(states));
  } finally { client.stop(); await stop(); db.db.close(); await rm(directory, { recursive: true, force: true }); }
});
