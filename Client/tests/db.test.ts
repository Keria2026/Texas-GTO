import test from 'node:test';
import assert from 'node:assert/strict';
import { core, database } from './helpers.ts';
import { CaptureDB } from '../extension/db.ts';
import { recordKey, sequence } from '../extension/model.ts';

test('queue survives reopen; ACK removes outbox only, never the event archive', async () => {
  const db = await database(); const s = await db.create(1, false);
  await db.append(s.id, 'capture_gap', { reason: 'start' }, { encoding: 'utf8', value: 'private-test-data' });
  assert.equal((await db.records(s.id))[0].payload, undefined);
  const name = db.db.name; db.db.close(); const reopened = await CaptureDB.open(core, name);
  assert.equal((await reopened.batch())?.events[0].sequence, '1');
  await reopened.acknowledge(s.id, '1', '1');
  assert.equal(await reopened.batch(), null);
  assert.equal((await reopened.records(s.id)).length, 1);
  assert.equal((await reopened.sessions())[0].pendingBytes, 0); reopened.db.close();
});

test('failed transaction leaves no advanced sequence, event or outbox', async () => {
  const db = await database(10); const s = await db.create(1, false);
  await assert.rejects(db.append(s.id, 'capture_gap', { reason: 'over-budget' }), /limit/);
  assert.equal((await db.sessions())[0].last, '0');
  assert.equal((await db.records(s.id)).length, 0);
  assert.equal(await db.batch(), null); db.db.close();
});

test('idle refresh reads only the original last event of an active fully acknowledged business source', async () => {
  const db = await database();
  try {
    const network = await db.create(1, false);
    assert.equal(await db.acknowledgedBusiness(), null);
    const business = await db.create(1, false, network.id);
    assert.equal(await db.acknowledgedBusiness(), null);
    const gap = { kind: 'capture_gap', table_id: null, hand_id: null, data: { reason: 'synthetic' } };
    await db.appendBusiness(business.id, [gap, gap]);
    await db.acknowledge(business.id, '1', '1');
    assert.equal(await db.acknowledgedBusiness(), null);
    await db.acknowledge(business.id, '2', '2');
    const original = (await db.records(business.id)).at(-1)!.record;
    const before = await db.sessions();
    assert.deepEqual((await db.acknowledgedBusiness())?.events, [original]);
    assert.deepEqual(await db.sessions(), before);
    assert.equal(await db.batch(), null);
    await db.update(business.id, 'interrupted');
    assert.equal(await db.acknowledgedBusiness(), null);
    await db.update(business.id, 'stopped');
    assert.equal(await db.acknowledgedBusiness(), null);
  } finally { db.db.close(); }
});

test('concurrent writers allocate durable contiguous sequences atomically', async () => {
  const db = await database(); const s = await db.create(1, true);
  await Promise.all(Array.from({ length: 80 }, () => db.append(s.id, 'capture_gap', { reason: 'synthetic' }, { encoding: 'base64', value: 'AAE=' })));
  const batch = await db.batch();
  assert.equal(batch?.events.length, 64); assert.equal(batch?.events[63].sequence, '64');
  assert.equal((await db.records(s.id))[0].payload?.value, 'AAE=');
  assert.ok(!JSON.stringify(batch).includes('AAE='));
  await assert.rejects(db.acknowledge(s.id, '65', '64'), /ACK/);
  await db.acknowledge(s.id, '64', '64'); await db.acknowledge(s.id, '64', '64');
  await assert.rejects(db.acknowledge(s.id, '63', '64'), /ACK/);
  assert.equal((await db.batch())?.events[0].sequence, '65');
  assert.equal((await db.recent(s.id))[0].sequence, '80'); db.db.close();
});

test('decimal sequences reject noncanonical input and sort through ten correctly', () => {
  for (const value of ['01', '-1', '1e2', '18446744073709551616', 1, null]) assert.throws(() => sequence(value));
  assert.ok(recordKey('s', '9')[1] < recordKey('s', '10')[1]);
});

test('a failure after event insertion rolls back the event and session update', async () => {
  const db = await database(); const s = await db.create(1, false);
  const tx = db.db.transaction('outbox', 'readwrite');
  tx.objectStore('outbox').add({ key: recordKey(s.id, '1'), source: s.id, bytes: 0 });
  await new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onerror = () => reject(tx.error); });
  await assert.rejects(db.append(s.id, 'capture_gap', { reason: 'must-roll-back' }));
  assert.equal((await db.sessions())[0].last, '0');
  assert.equal((await db.records(s.id)).length, 0); db.db.close();
});

test('changing receivers requeues the archive atomically without uploading raw payloads', async () => {
  const db = await database();
  try {
    const s = await db.create(1, true);
    await db.append(s.id, 'capture_gap', { reason: 'fixture' }, { encoding: 'utf8', value: 'local-secret' });
    await db.prepareBackend('ws://127.0.0.1:8766/v1/research/stream');
    await db.acknowledge(s.id, '1', '1');
    await db.prepareBackend('ws://127.0.0.1:8766/v1/research/stream');
    assert.equal(await db.batch(), null);
    await db.prepareBackend('ws://127.0.0.1:8765/v1/research/stream');
    const replay = await db.batch();
    assert.equal(replay?.source.ack, '0');
    assert.equal(replay?.source.pendingBytes, replay?.source.totalBytes);
    assert.equal(replay?.events[0].sequence, '1');
    assert.ok(!JSON.stringify(replay).includes('local-secret'));
    assert.equal((await db.records(s.id))[0].payload?.value, 'local-secret');
  } finally { db.db.close(); }
});

test('v1 databases upgrade without dropping archives and replay previously acknowledged metadata', async () => {
  const name = crypto.randomUUID();
  const old = await new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open(name, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore('sessions', { keyPath: 'id' });
      for (const name of ['events', 'outbox']) req.result.createObjectStore(name, { keyPath: 'key' }).createIndex('source', 'source');
    };
    req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error);
  });
  const legacy = new CaptureDB(old, core); const session = await legacy.create(1, false);
  await legacy.append(session.id, 'capture_gap', { reason: 'legacy-fixture' });
  await legacy.acknowledge(session.id, '1', '1'); old.close();
  const upgraded = await CaptureDB.open(core, name);
  try {
    assert.equal(upgraded.db.version, 2);
    assert.equal((await upgraded.records(session.id)).length, 1);
    await upgraded.prepareBackend('ws://127.0.0.1:8765/v1/research/stream');
    assert.equal((await upgraded.batch())?.events[0].sequence, '1');
    assert.equal((await upgraded.sessions())[0].ack, '0');
  } finally { upgraded.db.close(); }
});
