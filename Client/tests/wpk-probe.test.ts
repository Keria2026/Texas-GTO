import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { build } from 'esbuild';
import { installProbe } from '../extension/wpk/page-probe.js';
import { parseProbe, PROBE_SCHEMA } from '../extension/wpk/probe-model.ts';
import { ProbeStore } from '../extension/wpk/probe-store.ts';
import { WpkProbeBridge } from '../extension/wpk/probe-bridge.ts';
import { Collector } from '../extension/capture.ts';
import type { Target } from '../extension/capture.ts';
import { database, core } from './helpers.ts';

function page(globals: Record<string, unknown>, install = installProbe) {
  const reports: string[] = [], jobs: (() => void)[] = [];
  let tick = () => {}, expire = () => {};
  const context = vm.createContext({ ...globals, emit: (value: string) => reports.push(value),
    queueMicrotask: (job: () => void) => jobs.push(job),
    setInterval: (job: () => void) => { tick = job; return 1; },
    setTimeout: (job: () => void) => { expire = job; return 2; }, clearInterval() {}, clearTimeout() {} });
  vm.runInContext(`(${install.toString()})('emit','control')`, context);
  return { reports, context, flush() { while (jobs.length) jobs.shift()!(); }, tick() { tick(); }, expire() { expire(); },
    stop() { vm.runInContext('control.stop()', context); } };
}

test('decoded-dispatch hook preserves receiver, arguments, return identity and thrown errors', () => {
  const result = Promise.resolve(7), failure = Error('original failure'); let calls = 0;
  const original = function (this: unknown, name: string, payload: unknown) {
    assert.equal(this, instance); calls++;
    if (name === 'throw') throw failure;
    assert.equal(payload, data); return result;
  };
  const instance = { _dispatchMessageToCC: original };
  let getters = 0;
  const data = { userAciton: { action: 3, amount: 800 }, token: 'DO_NOT_EXPORT_TOKEN',
    userId: 'DO_NOT_EXPORT_ID', cards: [42, 43], get expensive() { getters++; throw Error('getter must not run'); } };
  const p = page({ HMFClient: { sharedInstance: () => instance } });
  assert.equal(instance._dispatchMessageToCC('WP_roundChangeNotify', data), result);
  assert.throws(() => instance._dispatchMessageToCC('throw', data), error => error === failure);
  assert.equal(calls, 2); p.flush();
  assert.equal(getters, 0);
  const reports = p.reports.map(parseProbe);
  const shape = reports.find(r => r.type === 'shape' && r.message === 'WP_roundChangeNotify');
  assert.ok(shape?.type === 'shape');
  assert.ok(shape.fields.some(f => f.path === 'arg1.userAciton.amount' && f.type === 'number'));
  assert.ok(shape.fields.some(f => f.path === 'arg1.cards' && f.type === 'redacted'));
  assert.ok(!p.reports.join('').includes('DO_NOT_EXPORT'));
  assert.ok(!p.reports.join('').includes('800'));
  p.stop(); assert.equal(instance._dispatchMessageToCC, original);
  assert.equal(vm.runInContext('typeof emit', p.context), 'undefined');
});

test('inherited dispatch methods and Cocos module lookup restore without an own-property leak', () => {
  const prototype = { _dispatchMessageToCC() { return 9; } };
  const instance = Object.create(prototype) as typeof prototype;
  const names: string[] = [];
  const p = page({ __require: (name: string) => { names.push(name); return { default: { sharedInstance: () => instance } }; } });
  assert.equal(instance._dispatchMessageToCC(), 9); p.flush();
  assert.deepEqual(names, ['HMFClient']);
  p.stop(); assert.equal(Object.hasOwn(instance, '_dispatchMessageToCC'), false);
});

test('replacement, stop and expiration disable observation without overwriting later page code', () => {
  const instance = { _dispatchMessageToCC() { return 1; } };
  const p = page({ HMFClient: { sharedInstance: () => instance } });
  instance._dispatchMessageToCC(); const replacement = () => 2;
  instance._dispatchMessageToCC = replacement; p.tick(); const count = p.reports.length; p.flush();
  assert.equal(p.reports.length, count); assert.equal(instance._dispatchMessageToCC, replacement);
  assert.ok(p.reports.some(r => JSON.parse(r).state === 'entry_replaced'));
  const missing = page({}); missing.expire();
  assert.ok(missing.reports.some(r => JSON.parse(r).state === 'entry_missing'));
  assert.equal(vm.runInContext('typeof control', missing.context), 'undefined');
});

test('compiled page function remains self-contained when serialized into the MAIN world', async () => {
  const output = await build({ entryPoints: ['extension/wpk/page-probe.js'], bundle: true, write: false, format: 'esm', target: 'chrome125', minify: true });
  const compiled = await import(`data:text/javascript;base64,${Buffer.from(output.outputFiles[0].text).toString('base64')}`) as { installProbe: typeof installProbe };
  const p = page({ HMFClient: { sharedInstance: () => ({ _dispatchMessageToCC() {} }) } }, compiled.installProbe);
  assert.ok(p.reports.some(r => JSON.parse(r).state === 'observing')); p.stop();
});

test('report validation rejects copied values, unrecognized names, oversized and forged reports', () => {
  const valid = { schema: PROBE_SCHEMA, type: 'shape', message: null, phase: 'after_dispatch_microtask',
    threw: false, fields: [{ path: 'arg0.userAciton', type: 'object' }], truncated: false };
  assert.equal(parseProbe(JSON.stringify(valid)).type, 'shape');
  for (const bad of [{ ...valid, payload: 'secret' }, { ...valid, message: 'UNKNOWN_PRIVATE_VALUE' },
    { ...valid, fields: [{ path: 'arg0', type: 'number', value: 42 }] },
    { ...valid, fields: [{ path: 'arg0.secret/path', type: 'number' }] }, { ...valid, type: 'business_event' }]) {
    assert.throws(() => parseProbe(JSON.stringify(bad)));
  }
  assert.throws(() => parseProbe(' '.repeat(16385)));
});

test('runtime bridge isolates source/context/origin, persists diagnostics separately and cleans up', async () => {
  const store = await ProbeStore.open(crypto.randomUUID()), db = await database();
  const calls: { target: Target; method: string; params?: object }[] = [];
  const api = { async attach() {}, async detach() {}, async sendCommand(target: Target, method: string, params?: object) {
    calls.push({ target, method, params }); return { result: { value: { status: 'installed' } } };
  } };
  const bridge = new WpkProbeBridge(api, store);
  const collector = new Collector(api, db, core, () => {});
  const target = { tabId: 7 };
  try {
    await collector.start(7, 'https://h5.wpk.com/', false); const source = collector.active!.id; await bridge.begin(source); await bridge.enable(target);
    assert.ok(calls.some(c => c.method === 'Runtime.enable'));
    for (const context of [
      { id: 1, origin: 'https://evil.example', auxData: { isDefault: true } },
      { id: 1, origin: 'https://h5.wpk.com', auxData: { isDefault: false } },
    ]) await bridge.handle(target, 'Runtime.executionContextCreated', { context });
    assert.equal(bridge.contexts.size, 0);
    await bridge.handle(target, 'Runtime.executionContextCreated', { context: { id: 1, origin: 'https://h5.wpk.com', auxData: { isDefault: true } } });
    const binding = (calls.find(c => c.method === 'Runtime.addBinding')!.params as { name: string }).name;
    const payload = JSON.stringify({ schema: PROBE_SCHEMA, type: 'state', state: 'observing' });
    await bridge.handle(target, 'Runtime.bindingCalled', { name: binding, executionContextId: 2, payload });
    assert.equal((await store.session(source))?.count, 1);
    await bridge.handle(target, 'Runtime.bindingCalled', { name: binding, executionContextId: 1, payload });
    assert.equal((await store.session(source))?.count, 2);
    assert.equal((await db.batch())?.events.length, 1); // Only the network attach gap is sent.
    await bridge.handle(target, 'Runtime.bindingCalled', { name: binding, executionContextId: 1, payload: '{"token":"not allowed"}' });
    assert.equal(bridge.contexts.size, 0); assert.ok(bridge.error);
    assert.ok(calls.some(c => c.method === 'Runtime.removeBinding'));
    await bridge.stop(); await collector.stop(); assert.equal((await store.session(source))?.state, 'stopped');
    const count = (await store.records(source)).length;
    await bridge.handle(target, 'Runtime.bindingCalled', { name: binding, executionContextId: 1, payload });
    assert.equal((await store.records(source)).length, count);
  } finally { await bridge.stop(); await collector.stop(); store.db.close(); db.db.close(); }
});

test('probe archive survives reopen and refuses over-budget appends atomically', async () => {
  const name = crypto.randomUUID(); let store = await ProbeStore.open(name);
  const report = { schema: PROBE_SCHEMA, type: 'state', state: 'probing' } as const;
  await Promise.all(Array.from({ length: 10 }, () => store.append('source', 'context', report)));
  store.db.close(); store = await ProbeStore.open(name);
  try {
    for (let i = 10; i < 256; i++) await store.append('source', 'context', report);
    await assert.rejects(store.append('source', 'context', report), /limit/);
    assert.equal((await store.session('source'))?.count, 256);
    assert.deepEqual((await store.records('source')).map(r => r.index), Array.from({ length: 256 }, (_, i) => i + 1));
  } finally { store.db.close(); }
});

test('bursty dispatch stays bounded, reports truncation and still invokes every original call', () => {
  let calls = 0;
  const original = () => { calls++; };
  const instance = { _dispatchMessageToCC: original };
  const p = page({ HMFClient: { sharedInstance: () => instance } });
  for (let i = 0; i < 1000; i++) instance._dispatchMessageToCC();
  p.flush();
  assert.equal(calls, 1000);
  assert.ok(p.reports.length <= 12);
  assert.ok(p.reports.some(r => JSON.parse(r).state === 'sample_limit'));
  assert.equal(instance._dispatchMessageToCC, original);
});
