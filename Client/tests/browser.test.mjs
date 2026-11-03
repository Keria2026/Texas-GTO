import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, mkdir, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join, dirname } from 'node:path';
import { execFileSync } from 'node:child_process';
import { chromium } from 'playwright';
import { WebSocketServer } from 'ws';
import { startBackend } from './reference-backend.ts';
const manifest = JSON.parse(await readFile('dist/manifest.json', 'utf8'));

async function until(check, timeout = 15000) {
  const end = Date.now() + timeout;
  while (!await check()) { if (Date.now() > end) throw Error('Browser condition timed out'); await new Promise(r => setTimeout(r, 100)); }
}
async function send(panel, message) {
  return panel.evaluate(async m => {
    const result = await chrome.runtime.sendMessage(m);
    if (!result.ok) throw Error(result.error);
    return result.value;
  }, message);
}
test('production package loads its service worker, WASM and sidebar without synthetic target access', { timeout: 30000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'tgto-production-'));
  let context;
  try {
    const extension = resolve('dist');
    context = await chromium.launchPersistentContext(join(directory, 'profile'), {
      headless: true, channel: 'chromium', executablePath: process.env.PW_CHROMIUM || undefined,
      ignoreDefaultArgs: ['--disable-extensions'],
      env: { ...process.env, XDG_CONFIG_HOME: join(directory, 'config'), XDG_CACHE_HOME: join(directory, 'cache') },
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, '--no-sandbox', '--disable-dev-shm-usage'],
    });
    const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
    const panel = await context.newPage(); await panel.goto(`chrome-extension://${worker.url().split('/')[2]}/sidepanel.html`);
    const status = await send(panel, { type: 'status' });
    assert.equal(status.wasm, 'ready'); assert.equal(status.tabs.length, 0);
    assert.equal(await panel.getByRole('heading', { name: 'TGTO', exact: true }).count(), 1);
    assert.equal(await panel.getByRole('button').count(), 1);
  } finally { await context?.close(); await rm(directory, { recursive: true, force: true }); }
});
test('minimal sidebar starts communication and invalidates stale recommendations', { timeout: 30000 }, async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PW_CHROMIUM || undefined, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage(); page.setDefaultTimeout(4000);
    await page.route('http://panel.test/**', async route => {
      const name = new URL(route.request().url()).pathname.slice(1);
      if (!['sidepanel.html', 'sidepanel.js', 'sidepanel.css'].includes(name)) return route.abort();
      await route.fulfill({ body: await readFile(`dist/${name}`), contentType: name.endsWith('.html') ? 'text/html' : name.endsWith('.js') ? 'application/javascript' : 'text/css' });
    });
    await page.addInitScript(() => {
      window.uiState = { sessions: [], transportState: 'ready', businessStatus: 'ready',
        business: { checkpoint: { hand_number: 7, street: 'flop', hero_cards: ['Ah', 'Ks'], board: ['Ad', '7c', '2h'],
          players: [{ player_id: 'hero', seat: 1, stack: 760 }, { player_id: 'player-2', seat: 2, stack: 800 }], turn: { player_id: 'hero' } } },
        recommendation: { status: 'ready', selected_action: 0, expires_at_ms: Date.now() + 60000,
          actions: [{ action: 'RAISE', amount_delta: 22, raise_to: 24 }] } };
      window.chrome = { runtime: { id: 'synthetic-ui', onMessage: { addListener(fn) { window.changed = fn; }, removeListener() {} }, sendMessage: async message => {
        if (message.type === 'start_game') { window.started = true; window.uiState.sessions = [{ state: 'active' }]; }
        if (window.hold && message.type === 'status') { window.hold = false; const value = structuredClone(window.uiState); return new Promise(resolve => { window.release = () => resolve({ ok: true, value }); }); }
        return { ok: true, value: structuredClone(window.uiState) };
      } } };
    });
    await page.goto('http://panel.test/sidepanel.html');
    const advice = page.locator('section[aria-labelledby="recommendation-title"]');
    await page.getByRole('button', { name: '开始对局', exact: true }).waitFor();
    assert.equal(await page.getByRole('button').count(), 1);
    await page.getByRole('button', { name: '开始对局', exact: true }).click();
    assert.equal(await page.evaluate(() => window.started), true);
    await advice.getByText('本次投入 22 分', { exact: true }).waitFor();
    for (const width of [320, 390, 600]) {
      await page.setViewportSize({ width, height: 920 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await page.setViewportSize({ width: 390, height: 1000 });
    const shot = resolve('.impeccable/review/sidebar-current.png'); await mkdir(dirname(shot), { recursive: true }); await page.screenshot({ path: shot, fullPage: true });
    await page.evaluate(() => { window.hold = true; window.changed({ type: 'state_changed' }, { id: 'synthetic-ui' }); });
    await page.waitForFunction(() => window.release);
    await page.evaluate(() => { window.uiState.recommendation = undefined; window.changed({ type: 'state_changed' }, { id: 'synthetic-ui' }); });
    await advice.getByText('等待当前牌局的有效建议。').waitFor();
    await page.evaluate(() => window.release());
    await page.waitForTimeout(100);
    assert.equal(await advice.locator('strong').count(), 0);
    await page.evaluate(() => { window.uiState.recommendation = { status: 'ready', expires_at_ms: Date.now() - 1, selected_action: 0, actions: [{ action: 'RAISE', amount_delta: 22, raise_to: 24 }] }; window.changed({ type: 'state_changed' }, { id: 'synthetic-ui' }); });
    await advice.getByText('建议已过期，等待新的牌桌状态。').waitFor();
    await page.evaluate(() => { window.uiState.transportState = 'retrying'; window.changed({ type: 'state_changed' }, { id: 'synthetic-ui' }); });
    await advice.getByText('等待连接本地服务。开始后会自动重连。').waitFor();
  } finally { await browser.close(); }
});
test('service worker restart resumes the authorized tab with a gap; explicit stop prevents recovery', { timeout: 45000 }, async () => {
  execFileSync(process.execPath, ['scripts/build.mjs', '--fixture']);
  const directory = await mkdtemp(join(tmpdir(), 'tgto-worker-recovery-'));
  const fixture = createServer((_req, res) => { res.setHeader('Content-Type', 'text/html'); res.end('<title>Synthetic recovery test</title>'); });
  await new Promise(resolve => fixture.listen(8767, '127.0.0.1', resolve));
  let context;
  try {
    const extension = resolve('dist-fixture');
    context = await chromium.launchPersistentContext(join(directory, 'profile'), {
      headless: true, channel: 'chromium', executablePath: process.env.PW_CHROMIUM || undefined,
      ignoreDefaultArgs: ['--disable-extensions'], args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, '--no-sandbox'],
    });
    const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
    const origin = `chrome-extension://${worker.url().split('/')[2]}`;
    const panel = await context.newPage(); await panel.goto(`${origin}/sidepanel.html`);
    const page = await context.newPage(); await page.goto('http://127.0.0.1:8767/');
    const tab = (await send(panel, { type: 'status' })).tabs[0].id;
    await send(panel, { type: 'start', tabId: tab, raw: false });
    const original = (await send(panel, { type: 'status' })).sessions[0].id;
    const cdp = await context.newCDPSession(panel);
    const stopWorker = async () => {
      const { targetInfos } = await cdp.send('Target.getTargets');
      const target = targetInfos.find(t => t.type === 'service_worker' && t.url.startsWith(origin));
      assert.ok(target); await cdp.send('Target.closeTarget', { targetId: target.targetId });
    };
    await stopWorker();
    await until(async () => (await send(panel, { type: 'status' })).sessions.some(s => s.state === 'active' && s.id !== original));
    const restored = await send(panel, { type: 'status' });
    assert.equal(restored.sessions.find(s => s.state === 'active').tabId, tab);
    assert.equal(restored.sessions.find(s => s.id === original).state, 'interrupted');
    assert.ok((await send(panel, { type: 'export', source: original, raw: false })).some(r => r.type === 'capture_gap' && r.data.reason.includes('worker_restart')));
    await send(panel, { type: 'stop' });
    await stopWorker();
    await until(async () => (await send(panel, { type: 'status' })).wasm === 'ready');
    assert.ok(!(await send(panel, { type: 'status' })).sessions.some(s => s.state === 'active'));
  } finally { await context?.close(); await new Promise(resolve => fixture.close(resolve)); await rm(directory, { recursive: true, force: true }); }
});
test('loadable MV3: real CDP frames, worker, iframe, closed sidebar, offline queue and browser restart', { timeout: 120000 }, async () => {
  execFileSync(process.execPath, ['scripts/build.mjs', '--fixture']);
  const production = JSON.parse(await readFile('dist/manifest.json', 'utf8'));
  assert.equal(production.manifest_version, 3);
  assert.ok(!(await readFile('dist/background.js', 'utf8')).includes('http://127.0.0.1:8767'));
  const directory = await mkdtemp(join(tmpdir(), 'tgto-browser-'));
  const profile = join(directory, 'profile');
  const syntheticWpk = await readFile('tests/fixtures/wpk-client.js', 'utf8');
  const fixture = createServer((req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    if (req.url === '/wpk-client.js') { res.setHeader('Content-Type', 'application/javascript'); res.end(syntheticWpk);
    } else if (req.url === '/worker.js') {
      res.setHeader('Content-Type', 'application/javascript');
      res.end("const ws = new WebSocket('ws://127.0.0.1:8767/worker'); ws.onopen=()=>ws.send('synthetic-worker');");
    } else if (req.url === '/iframe') {
      res.end("<script>const ws=new WebSocket('ws://127.0.0.1:8767/iframe');ws.onopen=()=>ws.send('synthetic-iframe')</script>");
    } else {
      res.setHeader('Content-Type', 'text/html');
      res.end(`<!doctype html><title>Synthetic WS capture fixture</title><p>Synthetic capture fixture</p><script src="/wpk-client.js"></script><script>
        window.startStreams=()=>{window.ws=new WebSocket('ws://127.0.0.1:8767/main?token=DO_NOT_PERSIST_QUERY');ws.onopen=()=>{ws.send('synthetic-main');ws.send(new Uint8Array([0,1,255]));};window.worker=new Worker('/worker.js');let iframe=document.createElement('iframe');iframe.src='/iframe';document.body.append(iframe);};
      </script>`);
    }
  });
  const echo = new WebSocketServer({ server: fixture });
  echo.on('connection', ws => ws.on('message', (data, binary) => ws.send(data, { binary })));
  await new Promise(r => fixture.listen(8767, '127.0.0.1', r));
  const extension = resolve('dist-fixture');
  const launch = () => chromium.launchPersistentContext(profile, {
    headless: true, channel: 'chromium', executablePath: process.env.PW_CHROMIUM || undefined,
    ignoreDefaultArgs: ['--disable-extensions'],
    env: { ...process.env, XDG_CONFIG_HOME: join(directory, 'config'), XDG_CACHE_HOME: join(directory, 'cache') },
    args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, '--no-sandbox', '--disable-dev-shm-usage'],
  });
  let context, backend;
  const errors = [];
  try {
    context = await launch();
    const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
    const id = worker.url().split('/')[2];
    const origin = `chrome-extension://${id}`;
    const options = { port: 8766, database: join(directory, 'receiver.sqlite'), token: 'synthetic-browser-token', origin };
    backend = await startBackend(options);
    let panel = await context.newPage(); panel.on('pageerror', e => errors.push(String(e)));
    await panel.goto(`${origin}/sidepanel.html`);
    await panel.getByText('WASM 核心', { exact: true }).count();
    const page = await context.newPage(); await page.goto('http://127.0.0.1:8767/');
    const tabs = await panel.evaluate(() => chrome.tabs.query({}));
    const tabId = tabs.find(t => t.url === 'http://127.0.0.1:8767/').id;

    // The reference receiver remains an explicit authenticated test transport.
    await send(panel, { type: 'connect', url: 'ws://127.0.0.1:8766/v1/research/stream', token: options.token });
    await send(panel, { type: 'start', tabId, raw: true });
    await until(async () => (await send(panel, { type: 'status' })).sessions.some(s => s.state === 'active'));
    await page.evaluate(() => window.startStreams());
    await until(async () => (await send(panel, { type: 'status' })).business?.checkpoint?.hand_number === 1);
    await page.evaluate(() => window.__wpkFixture.play());
    await until(async () => (await send(panel, { type: 'status' })).business?.checkpoint?.settlements?.length === 1);
    const businessStatus = await send(panel, { type: 'status' });
    const businessSource = businessStatus.business.id;
    assert.equal(businessStatus.business.checkpoint.settlements[0].status, 'reconciled');
    const businessRecords = await send(panel, { type: 'export', source: businessSource, raw: false });
    assert.ok(businessRecords.some(r => r.kind === 'settlement' && r.hand_id === 'table-1:2'));
    assert.equal(businessRecords.filter(r => r.kind === 'action_confirmed').length, 15);
    assert.ok(!JSON.stringify(businessRecords).includes('TOP_SECRET_FORBIDDEN'));
    let records;
    await until(async () => {
      const s = await send(panel, { type: 'status' });
      records = await send(panel, { type: 'export', source: s.sessions[0].id, raw: true });
      return records.filter(r => r.record.type === 'network_message').length >= 8;
    });
    const messages = records.filter(r => r.record.type === 'network_message');
    assert.ok(messages.some(r => r.payload?.value === 'synthetic-main'));
    assert.ok(messages.some(r => r.payload?.value === 'synthetic-worker'));
    assert.ok(messages.some(r => r.payload?.value === 'synthetic-iframe'));
    assert.ok(messages.some(r => r.record.data.opcode === 2 && r.record.data.byte_length === 3));
    assert.ok(!JSON.stringify(records.map(r => r.record)).includes('DO_NOT_PERSIST_QUERY'));
    await until(async () => { const s = (await send(panel, { type: 'status' })).sessions[0]; return s.last === s.ack; });
    await until(async () => { const s = (await send(panel, { type: 'status' })).business; return s.last === s.ack; });
    const beforeClose = BigInt((await send(panel, { type: 'status' })).sessions[0].last);
    await panel.close();
    await page.evaluate(() => window.ws.send('synthetic-after-sidebar-close'));
    panel = await context.newPage(); panel.on('pageerror', e => errors.push(String(e))); await panel.goto(`${origin}/sidepanel.html`);
    await until(async () => BigInt((await send(panel, { type: 'status' })).sessions[0].last) >= beforeClose + 2n);
    await backend.close(); backend = undefined;
    await page.evaluate(() => window.ws.send('synthetic-offline'));
    await until(async () => { const s = (await send(panel, { type: 'status' })).sessions[0]; return BigInt(s.last) > BigInt(s.ack); });
    const source = (await send(panel, { type: 'status' })).sessions[0].id;
    await context.close(); context = undefined;
    backend = await startBackend(options);
    context = await launch();
    panel = await context.newPage(); panel.on('pageerror', e => errors.push(String(e))); await panel.goto(`${origin}/sidepanel.html`);
    await until(async () => (await send(panel, { type: 'status' })).sessions[0]?.state === 'interrupted');
    const recovered = await send(panel, { type: 'export', source, raw: true });
    assert.ok((await send(panel, { type: 'export', source: businessSource, raw: false })).some(r => r.kind === 'settlement'));
    assert.ok(recovered.some(r => r.payload?.value === 'synthetic-offline'));
    // Chromium versions differ in whether detach is persisted before the browser exits.
    // Both paths must retain an explicit interruption, not merely the initial attach gap.
    const recoveryState = (await send(panel, { type: 'status' })).sessions[0];
    assert.ok(recoveryState.error === 'worker_restart' || recoveryState.error?.startsWith('Debugger detached:'));
    assert.ok(recovered.some(r => r.record.type === 'capture_gap'
      && (r.record.data.reason === 'worker_restart: continuous coverage unverified'
        || r.record.data.reason?.startsWith('Debugger detached:'))));
    assert.equal(await panel.evaluate(async () => (await chrome.storage.session.get('researchToken')).researchToken), undefined);
    await send(panel, { type: 'connect', url: 'ws://127.0.0.1:8766/v1/research/stream', token: options.token });
    await until(async () => { const s = (await send(panel, { type: 'status' })).sessions[0]; return s.last === s.ack; });
    await until(async () => { const s = (await send(panel, { type: 'status' })).business; return s.last === s.ack; });
    assert.ok(backend.db.prepare('SELECT body FROM events').all().some(r => JSON.parse(r.body).kind === 'settlement'));
    assert.ok(!JSON.stringify(backend.db.prepare('SELECT body FROM events').all()).includes('synthetic-main'));
    const shotDir = resolve('.impeccable/review'); await mkdir(shotDir, { recursive: true });
    await until(async () => await panel.getByText('已连接', { exact: true }).count() > 0);
    await until(async () => await panel.getByText('采集已中断，请重新开始', { exact: true }).count() > 0);
    for (const width of [320, 480]) {
      await panel.setViewportSize({ width, height: 1000 });

      assert.ok(await panel.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await panel.screenshot({ path: `${shotDir}/sidepanel-${width}.png`, fullPage: true });
    }
    assert.deepEqual(errors, []);
    // A root navigation must end the capture instead of silently following another document.
    const navigated = await context.newPage(); await navigated.goto('http://127.0.0.1:8767/');
    const newTabId = (await panel.evaluate(() => chrome.tabs.query({}))).find(t => t.url === 'http://127.0.0.1:8767/').id;
    await send(panel, { type: 'start', tabId: newTabId, raw: false });
    await navigated.reload();
    await until(async () => (await send(panel, { type: 'status' })).sessions[0].state === 'interrupted');
    console.log(JSON.stringify({ browser: context.browser()?.version(), realCapturedMessages: messages.length,
      recoveredRecords: recovered.length, sidebarClose: 'passed', offlineReplay: 'passed', browserRestart: 'passed' }));
  } finally {
    await context?.close(); await backend?.close();
    for (const ws of echo.clients) ws.terminate();
    await new Promise(r => echo.close(r)); await new Promise(r => fixture.close(r));
    await rm(directory, { recursive: true, force: true });
  }
});
