import test from 'node:test';
import assert from 'node:assert/strict';
import { database, core } from './helpers.ts';
import { Collector, endpoint, allowedPage } from '../extension/capture.ts';
import type { Target } from '../extension/capture.ts';

test('URL filtering does not accept lookalikes, credentials, or unrelated pages', () => {
  assert.equal(allowedPage('https://h5.wpk.com/'), true);
  for (const url of ['https://h5.wpk.com.evil.test/', 'http://h5.wpk.com/', 'https://other.test/']) assert.equal(allowedPage(url), false);
  assert.equal(endpoint('wss://game.wpk111.com:11111/wepoker/game4/battle/echo?token=secret'), 'wss://game.wpk111.com:11111/wepoker/game4/battle/echo');
  assert.equal(endpoint('wss://name:secret@game.wpk111.com:11111/wepoker/game4/battle/echo'), null);
});

test('text/binary messages, isolated child sessions, stop and unknown endpoints', async () => {
  const db = await database(); const calls: string[] = [];
  const api = { async attach() {}, async detach() {}, async sendCommand(_t: Target, method: string) { calls.push(method); } };
  const c = new Collector(api, db, core, () => {});
  await c.start(7, 'https://h5.wpk.com/', true); const id = c.active!.id;
  const root = { tabId: 7 }, child = { tabId: 7, sessionId: 'child' };
  await c.handle(root, 'Target.attachedToTarget', { sessionId: 'child', targetInfo: { url: 'https://h5.wpk.com/worker.js', type: 'worker' } });
  for (const target of [root, child]) {
    await c.handle(target, 'Network.webSocketCreated', { requestId: 'same-id', url: 'wss://game.wpk111.com:11111/wepoker/game4/battle/echo?private=yes' });
    await c.handle(target, 'Network.webSocketFrameReceived', { requestId: 'same-id', response: { opcode: 1, payloadData: '合成消息' }, timestamp: 1 });
    await c.handle(target, 'Network.webSocketFrameSent', { requestId: 'same-id', response: { opcode: 2, payloadData: 'AAH/' } });
  }
  await c.handle(root, 'Network.webSocketCreated', { requestId: 'unrelated', url: 'wss://unrelated.test/' });
  await c.handle(root, 'Network.webSocketFrameReceived', { requestId: 'unrelated', response: { opcode: 1, payloadData: 'ignored' } });
  const records = await db.records(id); const frames = records.filter(r => r.record.type === 'network_message');
  assert.equal(frames.length, 4); assert.equal(frames[0].record.data.byte_length, 12);
  assert.equal(frames[1].record.data.byte_length, 3);
  assert.notEqual(frames[0].record.data.connection_id, frames[2].record.data.connection_id);
  assert.equal(frames[1].payload?.value, 'AAH/');
  assert.ok(!JSON.stringify(records.map(r => r.record)).includes('private=yes'));
  assert.ok(calls.includes('Runtime.runIfWaitingForDebugger'));
  await assert.rejects(c.handle(root, 'Network.webSocketFrameReceived', { requestId: 'same-id', response: { opcode: 2, payloadData: '!' } }));
  await assert.rejects(c.handle(root, 'Network.webSocketFrameReceived', { requestId: 'same-id', response: { opcode: 4294967297, payloadData: 'no-wrap' } }));
  await assert.rejects(c.handle(root, 'Network.webSocketFrameReceived', { requestId: 'same-id', response: { opcode: 1, payloadData: 'x'.repeat(1024 * 1024 + 1) } }));
  await c.stop(); const count = (await db.records(id)).length;
  await c.handle(root, 'Network.webSocketFrameReceived', { requestId: 'same-id', response: { opcode: 1, payloadData: 'after-stop' } });
  assert.equal((await db.records(id)).length, count); db.db.close();
});

test('failed child initialization always resumes the paused target', async () => {
  const db = await database(); const methods: string[] = [];
  const api = { async attach() {}, async detach() {}, async sendCommand(t: Target, m: string) {
    methods.push(m); if (t.sessionId && m === 'Network.enable') throw Error('synthetic failure');
  } };
  const c = new Collector(api, db, core, () => {}); await c.start(1, 'https://h5.wpk.com/', false);
  await assert.rejects(c.handle({ tabId: 1 }, 'Target.attachedToTarget', { sessionId: 'c', targetInfo: { url: 'https://h5.wpk.com/w.js', type: 'worker' } }));
  assert.ok(methods.includes('Runtime.runIfWaitingForDebugger')); await c.stop(); db.db.close();
});
