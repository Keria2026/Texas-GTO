import test from 'node:test';
import assert from 'node:assert/strict';
import { database } from './helpers.ts';
import { recoverCapture } from '../extension/capture-recovery.ts';

test('restart recovery follows only the previously authorized tab and does not revive stopped capture', async () => {
  const db = await database(), session = await db.create(7, false);
  const intent = { source: session.id, tabId: 7, raw: false };
  const calls: unknown[] = [];
  const start = async (...args: unknown[]) => { calls.push(args); };
  const tab = async () => ({ url: 'https://h5.wpk.com/' });
  assert.equal(await recoverCapture(intent, [session], tab, start), true);
  assert.deepEqual(calls, [[7, 'https://h5.wpk.com/', false]]);
  for (const value of [undefined, {}, { ...intent, tabId: 8 }, { ...intent, raw: true }, { ...intent, source: 'other' }])
    assert.equal(await recoverCapture(value, [session], tab, start), false);
  assert.equal(await recoverCapture(intent, [], tab, start), false);
  assert.equal(await recoverCapture(intent, [session], async () => ({ url: 'https://unrelated.test/' }), start), false);
  assert.equal(await recoverCapture(intent, [session], async () => { throw Error('tab closed'); }, start), false);
  assert.equal(calls.length, 1);
  db.db.close();
});
