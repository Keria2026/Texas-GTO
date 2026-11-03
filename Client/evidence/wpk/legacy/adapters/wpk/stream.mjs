import { readFileSync, mkdirSync, appendFileSync, chmodSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { ObservedSnapshotSchema, ResearchSocketEventSchema } from '../../packages/protocol/src/index.ts';
import { installObserver } from './observer.mjs';

const token = readFileSync(process.argv[2] ?? 'artifacts/local/session.token', 'utf8').trim();
const sourceId = randomUUID();
mkdirSync('artifacts/local', { recursive: true });
const journal = 'artifacts/local/wpk-observations.jsonl';
appendFileSync(journal, '', { mode: 0o600 }); chmodSync(journal, 0o600);
const targets = await (await fetch('http://127.0.0.1:9223/json/list')).json();
const target = targets.find(t => t.type === 'page' && t.url === 'https://h5.wpk.com/');
if (!target) throw new Error('The WPK Edge page is not open');
const page = new WebSocket(target.webSocketDebuggerUrl);
const backend = new WebSocket('ws://127.0.0.1:8765/v1/research/stream');
let nextId = 0, draining = false, stopped = false;
const calls = new Map(), queue = [];
let outstanding = null, finish;
const lifetime = new Promise(resolve => { finish = resolve; });
function stop(reason) {
  if (stopped) return;
  stopped = true;
  console.error(reason);
  outstanding?.reject(new Error(reason));
  finish();
}
const opened = ws => new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = () => reject(new Error('Socket connection failed')); });
await Promise.all([opened(page), opened(backend)]);
function command(method, params = {}) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { calls.delete(id); reject(new Error('CDP timed out')); }, 10000);
    calls.set(id, { resolve, reject, timer }); page.send(JSON.stringify({ id, method, params }));
  });
}
async function drain() {
  if (draining) return;
  draining = true;
  try {
    while (queue.length && !stopped) {
      const snapshot = queue[0], id = randomUUID();
      const ack = new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('Observation acknowledgement timed out')), 15000);
        outstanding = { id, resolve: value => { clearTimeout(timer); resolve(value); }, reject: error => { clearTimeout(timer); reject(error); } };
      });
      backend.send(JSON.stringify({ type: 'request', id, method: 'POST', path: '/v1/observations', body: snapshot }));
      const result = await ack;
      if (!Number.isSafeInteger(result?.state_version) || result.state_version < 1
        || result.snapshot?.source_id !== sourceId || result.snapshot?.sequence !== snapshot.sequence) throw new Error('Invalid acknowledgement');
      appendFileSync(journal, JSON.stringify({ type: 'ack', source_id: sourceId, sequence: snapshot.sequence, state_version: result.state_version }) + '\n');
      queue.shift();
      console.log(JSON.stringify({ accepted_sequence: snapshot.sequence, state_version: result.state_version, hand: snapshot.hand_id, actions: snapshot.actions.length }));
    }
  } catch { stop('Collection stopped; unacknowledged observations remain in the local journal.'); }
  finally { draining = false; }
}
page.onmessage = event => {
  const m = JSON.parse(event.data), call = calls.get(m.id);
  if (call) {
    clearTimeout(call.timer); calls.delete(m.id);
    m.error ? call.reject(new Error('CDP operation failed')) : call.resolve(m.result);
  } else if (m.method === 'Runtime.executionContextsCleared' || m.method === 'Inspector.detached') {
    stop('WPK execution context changed; stop and resynchronize the source.');
  } else if (m.method === 'Runtime.bindingCalled' && m.params.name === 'texasResearchEmit') {
    try {
      const snapshot = ObservedSnapshotSchema.parse(JSON.parse(m.params.payload));
      if (queue.length >= 256) throw new Error('Queue overflow');
      appendFileSync(journal, JSON.stringify({ type: 'observation', snapshot }) + '\n');
      queue.push(snapshot); void drain();
    } catch { stop('Invalid observation or queue overflow; source requires resynchronization.'); }
  }
};
const ready = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('Backend authentication timed out')), 6000);
  backend.onmessage = event => {
    try {
      const m = ResearchSocketEventSchema.parse(JSON.parse(event.data));
      if (m.type === 'ready') { clearTimeout(timer); resolve(); }
      if (m.type === 'response' && m.id === outstanding?.id) {
        m.status < 400 ? outstanding.resolve(m.body) : outstanding.reject(new Error('Backend rejected observation'));
        outstanding = null;
      }
    } catch { stop('Invalid backend response; stop and resynchronize.'); }
  };
});
backend.send(JSON.stringify({ type: 'auth', token }));
await ready;
backend.onclose = () => stop('Backend disconnected; current results must be considered stale.');
page.onclose = () => stop('WPK page disconnected; collection stopped.');
process.on('SIGINT', () => stop('Collector stopped by user.'));
process.on('SIGTERM', () => stop('Collector stopped.'));
try {
  await command('Runtime.enable');
  await command('Runtime.addBinding', { name: 'texasResearchEmit' });
  const installed = await command('Runtime.evaluate', { expression: `(${installObserver.toString()})(${JSON.stringify(sourceId)})`, returnByValue: true });
  if (installed.exceptionDetails) throw new Error('WPK observer initialization failed');
  console.log('Read-only WPK research stream connected. Credentials are not sent to the page.');
  await lifetime;
} finally {
  if (page.readyState === WebSocket.OPEN) {
    await command('Runtime.evaluate', { expression: 'window.__texasResearchObserver?.stop()' }).catch(() => {});
    await command('Runtime.removeBinding', { name: 'texasResearchEmit' }).catch(() => {});
  }
  page.close(); backend.close();
}
