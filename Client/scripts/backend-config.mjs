export function backendConfig(remote = '') {
  const local = ['ws://127.0.0.1:8765/v1/research/stream', 'ws://127.0.0.1:8766/v1/research/stream'];
  if (!remote) return { urls: local, permissions: [], connect: local.map(value => new URL(value).origin) };
  const url = new URL(remote);
  if (url.protocol !== 'wss:' || url.username || url.password || url.search || url.hash
    || url.pathname !== '/v1/research/stream' || url.href !== remote
    || !/^[a-z0-9.-]+$/.test(url.hostname)) throw Error('TGTO_BACKEND_URL must be a canonical wss://host[:port]/v1/research/stream URL without credentials/query/fragment');
  // A server build has only its configured endpoint; local tools remain available in a default build.
  return { urls: [url.href], permissions: [`https://${url.host}/*`], connect: [url.origin] };
}

export function fixtureBackendConfig(port = '8766') {
  if (!/^[1-9][0-9]{0,4}$/.test(port) || Number(port) > 65535 || Number(port) === 8765)
    throw Error('Fixture backend requires a separate loopback port, never production port 8765');
  const origin = `ws://127.0.0.1:${port}`;
  return { urls: [`${origin}/v1/research/stream`], permissions: [], connect: [origin] };
}
