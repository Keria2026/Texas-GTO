// Local protocol reference receiver, not a training/algorithm service.
import { createServer } from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import { timingSafeEqual, randomBytes } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync, chmodSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { WebSocketServer, WebSocket } from 'ws';
import { SCHEMA, CAPABILITY, BUSINESS_SCHEMA, BUSINESS_CAPABILITY, sequence } from '../extension/model.ts';
import type { RecordEnvelope, BusinessRecord } from '../extension/model.ts';
import { loadCore, wpkCall } from '../extension/core.ts';
const businessCore = await loadCore(new Uint8Array(readFileSync(new URL('../target/wasm32-unknown-unknown/release/tgto_capture_core.wasm', import.meta.url))));
function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).sort(([a],[b]) => a.localeCompare(b)).map(([k,v]) => [k,stable(v)]));
  return value;
}

const kinds = new Set(['network_message', 'capture_gap', 'socket_open', 'socket_closed', 'socket_error']);
function validate(event: unknown, source: string): RecordEnvelope {
  if (!event || typeof event !== 'object') throw Error('Invalid event');
  const r = event as RecordEnvelope;
  if (Object.keys(r).sort().join(',') !== 'captured_at,data,schema,sequence,source_id,type'
    || r.schema !== SCHEMA || r.source_id !== source || !kinds.has(r.type)
    || !sequence(r.sequence) || typeof r.captured_at !== 'string' || !Number.isFinite(Date.parse(r.captured_at))) throw Error('Invalid envelope');
  if (!r.data || typeof r.data !== 'object' || Array.isArray(r.data)) throw Error('Invalid data');
  const allowed = new Set(['connection_id', 'endpoint', 'direction', 'opcode', 'byte_length', 'sha256', 'monotonic_time', 'reason']);
  for (const [key, value] of Object.entries(r.data)) {
    if (!allowed.has(key)) throw Error('Unexpected field (raw payloads are forbidden)');
    if (['opcode', 'byte_length', 'monotonic_time'].includes(key)) {
      if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw Error('Invalid numeric field');
    } else if (typeof value !== 'string' || value.length > 512) throw Error('Invalid string field');
  }
  if (r.data.endpoint) {
    const url = new URL(r.data.endpoint);
    if (!['ws:', 'wss:'].includes(url.protocol) || url.search || url.hash || url.username || url.password) throw Error('Unsafe endpoint');
  }
  if (r.type === 'network_message' && (!['received', 'sent'].includes(r.data.direction ?? '')
    || !/^[a-f0-9]{64}$/.test(r.data.sha256 ?? '') || !Number.isInteger(r.data.byte_length)
    || (r.data.byte_length ?? 0) > 1024 * 1024 || ((r.data.opcode ?? 0) >= 8 && (r.data.byte_length ?? 0) > 125)
    || ![1, 2, 8, 9, 10].includes(r.data.opcode ?? -1))) throw Error('Invalid network message');
  return r;
}
export async function startBackend(options: { port: number; database: string; token: string; origin: string }) {
  if (!/^chrome-extension:\/\/[a-p]{32}$/.test(options.origin)) throw Error('Set one exact extension Origin');
  if (options.token.length < 16 || options.token.length > 4096) throw Error('Invalid research token length');
  const db = new DatabaseSync(options.database);
  if (options.database !== ':memory:') chmodSync(options.database, 0o600);
  db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS events(source TEXT, sequence TEXT, body TEXT NOT NULL, PRIMARY KEY(source,sequence)); CREATE TABLE IF NOT EXISTS heads(source TEXT PRIMARY KEY, sequence TEXT NOT NULL)');
  const http = createServer((_req, res) => { res.writeHead(404); res.end(); });
  const wss = new WebSocketServer({ server: http, path: '/v1/research/stream', maxPayload: 256 * 1024,
    verifyClient: (info: { origin: string }) => info.origin === options.origin });
  wss.on('connection', ws => {
    let authenticated = false;
    const deadline = setTimeout(() => ws.close(1008, 'Authentication timeout'), 5000);
    ws.on('close', () => clearTimeout(deadline));
    ws.on('error', () => {});
    ws.on('message', (bytes, binary) => {
      try {
        if (binary) throw Error('Text JSON required');
        const m = JSON.parse(bytes.toString()) as Record<string, unknown>;
        if (!authenticated) {
          const supplied = typeof m.token === 'string' ? Buffer.from(m.token) : Buffer.alloc(0);
          const expected = Buffer.from(options.token);
          if (m.type !== 'auth' || supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) throw Error('Unauthorized');
          authenticated = true; clearTimeout(deadline);
          ws.send(JSON.stringify({ type: 'ready', schema_version: '1.2', capabilities: [CAPABILITY, BUSINESS_CAPABILITY] })); return;
        }
        if (m.type === 'ping') { ws.send('{"type":"pong"}'); return; }
        const business = m.schema === BUSINESS_SCHEMA && m.type === 'capture.append';
        if ((!business && (m.schema !== SCHEMA || m.type !== 'network.append')) || typeof m.source_id !== 'string'
          || !/^[a-zA-Z0-9-]{1,80}$/.test(m.source_id) || !Array.isArray(m.events) || !m.events.length || m.events.length > 64) throw Error('Invalid batch');
        const events = m.events.map(e => {
          if (!business) return validate(e, m.source_id as string);
          wpkCall(businessCore, { op: 'validate', record: e });
          const record = e as BusinessRecord;
          if (record.source_id !== m.source_id || !Number.isFinite(Date.parse(record.captured_at))) throw Error('Business scope/time mismatch');
          return record;
        });
        for (let i = 1; i < events.length; i++) if (sequence(events[i].sequence) !== sequence(events[i - 1].sequence) + 1n) throw Error('Noncontiguous batch');
        db.exec('BEGIN IMMEDIATE');
        try {
          let last = BigInt((db.prepare('SELECT sequence FROM heads WHERE source=?').get(m.source_id) as { sequence: string } | undefined)?.sequence ?? '0');
          for (const event of events) {
            const body = JSON.stringify(stable(event));
            const existing = db.prepare('SELECT body FROM events WHERE source=? AND sequence=?').get(m.source_id, event.sequence) as { body: string } | undefined;
            if (existing) { if (JSON.stringify(stable(JSON.parse(existing.body))) !== body) throw Error('Conflicting sequence'); continue; }
            if (sequence(event.sequence) !== last + 1n) throw Error('Sequence gap');
            db.prepare('INSERT INTO events VALUES(?,?,?)').run(m.source_id, event.sequence, body);
            last++;
          }
          db.prepare('INSERT INTO heads VALUES(?,?) ON CONFLICT(source) DO UPDATE SET sequence=excluded.sequence').run(m.source_id, String(last));
          db.exec('COMMIT');
        } catch (error) { db.exec('ROLLBACK'); throw error; }
        ws.send(JSON.stringify({ schema: business ? BUSINESS_SCHEMA : SCHEMA, type: business ? 'capture.ack' : 'network.ack',
          source_id: m.source_id, through_sequence: events.at(-1)!.sequence }));
      } catch {
        if (ws.readyState === WebSocket.OPEN) ws.send('{"type":"error","code":"invalid_or_conflicting_request"}');
        ws.close(1008, 'Request rejected');
      }
    });
  });
  await new Promise<void>((resolve, reject) => { http.once('error', reject); http.listen(options.port, '127.0.0.1', resolve); });
  return { port: (http.address() as { port: number }).port, db, wss,
    async close() {
      for (const client of wss.clients) client.terminate();
      await new Promise<void>(resolve => wss.close(() => resolve()));
      await new Promise<void>(resolve => http.close(() => resolve()));
      db.close();
    } };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const directory = resolve('artifacts/reference-backend'); mkdirSync(directory, { recursive: true, mode: 0o700 });
  chmodSync(directory, 0o700);
  const tokenFile = `${directory}/session.token`;
  try { writeFileSync(tokenFile, randomBytes(32).toString('base64url'), { flag: 'wx', mode: 0o600 }); } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
  }
  chmodSync(tokenFile, 0o600);
  const origin = process.env.TGTO_ORIGIN ?? '';
  const app = await startBackend({ port: 8766, database: `${directory}/capture.sqlite`, token: readFileSync(tokenFile, 'utf8').trim(), origin });
  console.log(`Reference receiver listening on 127.0.0.1:${app.port}; token file: ${tokenFile}`);
  process.once('SIGINT', () => { void app.close().then(() => process.exit(0)); });
  process.once('SIGTERM', () => { void app.close().then(() => process.exit(0)); });
}
