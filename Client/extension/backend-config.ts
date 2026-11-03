// Production endpoints are embedded at build time along with matching manifest permissions.
// Node tests retain the two explicit loopback receivers.
declare const __BACKEND_URLS__: string[];
export const BACKENDS: readonly string[] = typeof __BACKEND_URLS__ === 'undefined'
  ? ['ws://127.0.0.1:8765/v1/research/stream', 'ws://127.0.0.1:8766/v1/research/stream']
  : __BACKEND_URLS__;
