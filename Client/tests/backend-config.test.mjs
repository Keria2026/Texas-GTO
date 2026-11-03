import test from 'node:test';
import assert from 'node:assert/strict';
import { backendConfig, fixtureBackendConfig } from '../scripts/backend-config.mjs';

test('build config pins one WSS endpoint and its CSP/host permissions', () => {
  const url = 'wss://analysis.example.org/v1/research/stream';
  assert.deepEqual(backendConfig(url), { urls: [url], permissions: ['https://analysis.example.org/*'], connect: ['wss://analysis.example.org'] });
  assert.equal(backendConfig().urls.length, 2);
  for (const value of ['ws://analysis.example.org/v1/research/stream',
    'wss://user:secret@analysis.example.org/v1/research/stream',
    `${url}?token=secret`, `${url}#fragment`, 'wss://analysis.example.org/',
    'wss://analysis.example.org/v1/../v1/research/stream', `${url}\n`, 'wss://*.example.org/v1/research/stream']) {
    assert.throws(() => backendConfig(value));
  }
});

test('compiled server package resolves to only the configured endpoint', async () => {
  const { build } = await import('esbuild');
  const url = 'wss://analysis.example.com/v1/research/stream';
  const output = await build({ entryPoints: ['extension/backend-config.ts'], bundle: true, write: false,
    format: 'esm', minify: true, define: { __BACKEND_URLS__: JSON.stringify(backendConfig(url).urls) } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(output.outputFiles[0].text).toString('base64')}`);
  assert.deepEqual(module.BACKENDS, [url]);
});

test('synthetic fixture builds isolate their endpoint from the production port', () => {
  assert.deepEqual(fixtureBackendConfig('47891'), {
    urls: ['ws://127.0.0.1:47891/v1/research/stream'], permissions: [], connect: ['ws://127.0.0.1:47891'],
  });
  assert.equal(fixtureBackendConfig().urls[0], 'ws://127.0.0.1:8766/v1/research/stream');
  assert.equal(backendConfig().urls[0], 'ws://127.0.0.1:8765/v1/research/stream');
  for (const port of ['8765', '0', '01', '65536', '123/stream', 'abc', '']) assert.throws(() => fixtureBackendConfig(port));
});

test('token-free local mode does not remove authentication from remote builds', async () => {
  const { build } = await import('esbuild');
  const url = 'wss://analysis.example.com/v1/research/stream';
  const output = await build({ entryPoints: ['extension/transport.ts'], bundle: true, write: false,
    format: 'esm', define: { __BACKEND_URLS__: JSON.stringify([url]) } });
  const module = await import(`data:text/javascript;base64,${Buffer.from(output.outputFiles[0].text).toString('base64')}`);
  const socket = new module.ResearchSocket({}, () => {});
  assert.throws(() => socket.connect(url), /remote backend requires/);
});
