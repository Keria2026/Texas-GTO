// Explicit browser integration; requires both checkouts and a browser permitting test extensions.
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { once } from 'node:events';
import { createServer as createPortProbe } from 'node:net';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join } from 'node:path';
import { until } from './helpers.ts';
const root = resolve(process.env.TEXAS_GTO_ROOT ?? '..');
const python = join(root, '.venv/bin/python');

test('token-free install, server recovery and browser restart → Python business observation', { timeout: 90000 }, async () => {
  const { chromium } = await import('playwright');
  const { createServer } = await import('node:http');
  const { WebSocketServer } = await import('ws');
  const probe = createPortProbe();
  await new Promise(r => probe.listen(0, '127.0.0.1', r));
  const backendPort = probe.address().port;
  await new Promise(r => probe.close(r));
  execFileSync(process.execPath, ['scripts/build.mjs', '--fixture'], {
    env: { ...process.env, TGTO_FIXTURE_BACKEND_PORT: String(backendPort) },
  });
  const directory = await mkdtemp(join(tmpdir(), 'tgto-python-browser-'));
  const wpkClient = await readFile(new URL('./fixtures/wpk-client.js', import.meta.url), 'utf8');
  const fixture = createServer((_req, res) => {
    res.setHeader('Content-Type', 'text/html');
    res.end(`<!doctype html><title>Synthetic capture</title><script>${wpkClient}
window.startStream=()=>{window.ws=new WebSocket("ws://127.0.0.1:8767/main");ws.onopen=()=>ws.send("SYNTHETIC_ONLY");}</script>`);
  });
  const echo = new WebSocketServer({ server: fixture });
  echo.on('connection', ws => ws.on('message', bytes => ws.send(bytes)));
  await new Promise(r => fixture.listen(8767, '127.0.0.1', r));
  let context, server;
  try {
    const extension = resolve('dist-fixture');
    context = await chromium.launchPersistentContext(join(directory, 'profile'), {
      headless: true, channel: 'chromium', executablePath: process.env.PW_CHROMIUM || undefined,
      ignoreDefaultArgs: ['--disable-extensions'],
      env: { ...process.env, XDG_CONFIG_HOME: join(directory, 'config'), XDG_CACHE_HOME: join(directory, 'cache') },
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, '--no-sandbox', '--disable-dev-shm-usage'],
    });
    const worker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
    const browserOrigin = `chrome-extension://${worker.url().split('/')[2]}`;
    const panel = await context.newPage(); await panel.goto(`${browserOrigin}/sidepanel.html`);
    const command = value => panel.evaluate(async message => {
      const response = await chrome.runtime.sendMessage(message);
      if (!response.ok) throw Error(response.error);
      return response.value;
    }, value);
    async function startServer() {
      let diagnostics = '';
      const environment = { ...process.env, TEXAS_PORT: String(backendPort), TEXAS_DATA_DIR: join(directory, 'server') };
      delete environment.TEXAS_API_TOKEN; delete environment.TEXAS_ALLOWED_ORIGINS;
      server = spawn(python, ['-m', 'texas_analysis'], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'], env: environment });
      server.stderr.on('data', b => { diagnostics += b.toString(); });
      await until(() => {
        if (server.exitCode !== null) throw Error(`Python startup failed: ${diagnostics}`);
        return diagnostics.includes('Uvicorn running on');
      });
    }
    async function stopServer() {
      if (server && server.exitCode === null) { const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited; }
    }
    // The single start action authorizes local communication and capture.
    await startServer();
    assert.equal(await panel.locator('input[type="password"]').count(), 0);
    const page = await context.newPage(); await page.goto('http://127.0.0.1:8767/');
    const tabId = (await panel.evaluate(() => chrome.tabs.query({}))).find(t => t.url === 'http://127.0.0.1:8767/').id;
    await panel.getByRole('button', { name: '开始对局', exact: true }).click();
    await until(async () => (await command({ type: 'status' })).transportState === 'ready');
    await page.evaluate(() => window.startStream());
    await until(async () => (await command({ type: 'status' })).businessStatus === 'ready');
    await page.evaluate(() => window.__wpkFixture.play());
    await until(async () => {
      const s = await command({ type: 'status' });
      return BigInt(s.sessions[0].last) >= 4n && s.sessions[0].last === s.sessions[0].ack
        && s.serverCapture?.through_sequence === s.business?.last && s.serverCapture?.state.closed
        && s.serverProfiles?.through_sequence === s.business?.last;
    });

    assert.equal((await command({ type: 'status' })).serverCapture.analysis.actions.length, 0);
    const profiles = (await command({ type: 'status' })).serverProfiles;
    assert.equal(profiles.identity_scope, 'origin_source');
    assert.ok(profiles.players.some(p => p.action_observations > 0));
    assert.equal(await panel.locator('section[aria-labelledby="profiles-title"]').count(), 0);
    await stopServer();
    await until(async () => (await command({ type: 'status' })).transportState === 'retrying');
    assert.equal((await command({ type: 'status' })).serverProfiles, undefined);
    await panel.getByText('正在重连', { exact: true }).waitFor();
    await startServer();
    await until(async () => (await command({ type: 'status' })).transportState === 'ready');
    await until(async () => (await command({ type: 'status' })).serverProfiles?.through_sequence === profiles.through_sequence);
    assert.deepEqual((await command({ type: 'status' })).serverProfiles, profiles);
    await command({ type: 'disconnect' });
    assert.equal((await command({ type: 'status' })).serverCapture, undefined);
    assert.equal((await command({ type: 'status' })).serverProfiles, undefined);
    await context.close();
    context = await chromium.launchPersistentContext(join(directory, 'profile'), {
      headless: true, channel: 'chromium', executablePath: process.env.PW_CHROMIUM || undefined,
      ignoreDefaultArgs: ['--disable-extensions'],
      env: { ...process.env, XDG_CONFIG_HOME: join(directory, 'config'), XDG_CACHE_HOME: join(directory, 'cache') },
      args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`, '--no-sandbox', '--disable-dev-shm-usage'],
    });
    const restoredWorker = context.serviceWorkers()[0] ?? await context.waitForEvent('serviceworker');
    const restored = await context.newPage();
    await restored.goto(`chrome-extension://${restoredWorker.url().split('/')[2]}/sidepanel.html`);
    assert.equal((await restored.evaluate(() => chrome.runtime.sendMessage({ type: 'status' }))).value.transportState, 'disconnected');
    assert.equal(await restored.locator('input[type="password"]').count(), 0);

  } finally {
    await context?.close();
    if (server && server.exitCode === null) { const exited = once(server, 'exit'); server.kill('SIGTERM'); await exited; }
    for (const ws of echo.clients) ws.terminate();
    await new Promise(r => echo.close(r)); await new Promise(r => fixture.close(r));
    await rm(directory, { recursive: true, force: true });
  }
});
