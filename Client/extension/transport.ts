import { parseRecommendation, RECOMMENDATIONS_CAPABILITY, SEARCH_CAPABILITY, PR_CAPABILITY } from './recommendation.ts';
import type { Recommendation } from './recommendation.ts';
import { parseCaptureState, CAPTURE_STATE_CAPABILITY } from './capture-state.ts';
import type { CaptureState } from './capture-state.ts';
import { parseCaptureProfiles, CAPTURE_PROFILES_CAPABILITY } from './capture-profiles.ts';
import type { CaptureProfiles } from './capture-profiles.ts';
import type { CaptureDB } from './db.ts';
import { CAPABILITY, SCHEMA, BUSINESS_CAPABILITY, BUSINESS_SCHEMA, sequence } from './model.ts';

import { BACKENDS } from './backend-config.ts';
import { parseStatus, STATUS_CAPABILITY } from './server-status.ts';
import type { NetworkStatus } from './server-status.ts';
export { BACKENDS } from './backend-config.ts';
type Flight = { source: string; through: string; refresh?: boolean };
export class ResearchSocket {
  db: CaptureDB;
  report: (state: string, error?: string) => void;
  socket?: WebSocket;
  enabled = false;
  ready = false;
  url = '';
  token = '';
  flight?: Flight;
  serverStatus?: NetworkStatus;
  captureStates = new Map<string, CaptureState>();
  captureProfiles = new Map<string, CaptureProfiles>();
  recommendations = new Map<string, Recommendation>();
  supportsStatus = false;
  busy = false;
  dirty = false;
  refreshPending = false;
  capabilities = new Set<string>();
  lastSource?: string;
  generation = 0;
  retries = 0;
  retryTimer?: ReturnType<typeof setTimeout>;
  deadline?: ReturnType<typeof setTimeout>;
  heartbeat?: ReturnType<typeof setInterval>;
  constructor(db: CaptureDB, report: (state: string, error?: string) => void) {
    this.db = db; this.report = report;
  }
  connect(url: string, token = ''): void {
    if (!BACKENDS.includes(url)) throw Error('Backend URL is not allowed');
    if (token.length > 4096 || (!token && !url.startsWith('ws://127.0.0.1:'))) throw Error('A remote backend requires a valid research token');
    this.stop(); this.url = url; this.token = token;
    const generation = this.generation;
    this.report('connecting');
    void this.db.prepareBackend(url).then(() => {
      if (generation !== this.generation) return;
      this.enabled = true; this.open();
    }).catch(() => {
      if (generation === this.generation) this.halt('error', 'Cannot prepare the durable queue for this server');
    });
  }
  stop(): void {
    this.enabled = false; this.ready = false; this.flight = undefined; this.generation++;
    this.token = ''; this.serverStatus = undefined; this.supportsStatus = false; this.captureStates.clear(); this.captureProfiles.clear(); this.recommendations.clear(); this.capabilities.clear();
    clearTimeout(this.retryTimer); this.clearTimers();
    this.socket?.close(); this.socket = undefined;
    this.report('disconnected');
  }
  clearTimers(): void {
    clearTimeout(this.deadline); clearInterval(this.heartbeat);
  }
  open(): void {
    if (!this.enabled) return;
    const generation = ++this.generation;
    const ws = new WebSocket(this.url);
    this.socket = ws; this.ready = false; this.flight = undefined;
    this.refreshPending = true;
    this.serverStatus = undefined; this.supportsStatus = false; this.captureStates.clear(); this.captureProfiles.clear(); this.recommendations.clear(); this.capabilities.clear();
    this.report('connecting');
    this.deadline = setTimeout(() => ws.close(), 5000);
    ws.onopen = () => {
      if (generation !== this.generation) return;
      ws.send(JSON.stringify({ type: 'auth', ...(this.token ? { token: this.token } : {}), capabilities: [STATUS_CAPABILITY, CAPTURE_STATE_CAPABILITY, CAPTURE_PROFILES_CAPABILITY, RECOMMENDATIONS_CAPABILITY, SEARCH_CAPABILITY, PR_CAPABILITY] }));
    };
    let received = Promise.resolve();
    ws.onmessage = event => {
      received = received.then(async () => {
        if (generation !== this.generation || ws.readyState !== WebSocket.OPEN) return;
        if (typeof event.data !== 'string' || event.data.length > 256 * 1024) throw Error('Invalid server frame');
        const message = JSON.parse(event.data) as Record<string, unknown>;
        if (message.type === 'ready') {
          if (this.ready) throw Error('Duplicate ready');
          if (message.schema_version !== '1.2' || !Array.isArray(message.capabilities)
            || !message.capabilities.some(c => c === CAPABILITY || c === BUSINESS_CAPABILITY)) {
            this.halt('unsupported', 'Backend supports neither network nor business events; queued records are retained'); return;
          }
          this.supportsStatus = message.capabilities.includes(STATUS_CAPABILITY);
          this.capabilities = new Set(message.capabilities.filter(c => typeof c === 'string'));
          clearTimeout(this.deadline); this.ready = true; this.retries = 0;
          this.report('ready');
          let lastPong = Date.now();
          this.heartbeat = setInterval(() => {
            if (Date.now() - lastPong > 45000) { ws.close(); return; }
            if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
          }, 20000);
          // Pong handling owns a connection-local clock, not persisted state.
          this.pong = () => { lastPong = Date.now(); };
          await this.pump();
        } else if (message.type === 'pong') {
          if (!this.ready) throw Error('Pong before ready');
          this.pong();
        } else if (message.type === 'network.ack' || message.type === 'capture.ack') {
          if (!this.ready || typeof message.source_id !== 'string') throw Error('Invalid ACK envelope');
          const through = String(sequence(message.through_sequence));
          const source = (await this.db.sessions()).find(s => s.id === message.source_id);
          if (generation !== this.generation || ws.readyState !== WebSocket.OPEN) return;
          if (!source) throw Error('ACK source is unknown');
          const business = source.stream === 'business';
          if (message.schema !== (business ? BUSINESS_SCHEMA : SCHEMA)
            || message.type !== (business ? 'capture.ack' : 'network.ack')) throw Error('ACK stream mismatch');
          const flight = this.flight;
          if (through === source.ack) {
            // A one-event refresh intentionally repeats the durable ACK; older duplicates
            // must never finish a different, newer in-flight batch.
            if (!flight?.refresh || flight.source !== source.id || flight.through !== through) return;
          }
          if (!flight || flight.source !== message.source_id) throw Error('ACK does not match the in-flight source');
          if (!flight.refresh) await this.db.acknowledge(flight.source, through, flight.through);
          else if (through !== flight.through) throw Error('Refresh ACK exceeds the replayed event');
          if (generation !== this.generation || ws.readyState !== WebSocket.OPEN) return;
          clearTimeout(this.deadline); this.flight = undefined;
          this.report('ready'); await this.pump();
        } else if (message.type === 'capture.state') {
          if (!this.ready || !this.capabilities.has(CAPTURE_STATE_CAPABILITY)) throw Error('Unnegotiated capture state');
          const result = parseCaptureState(message, this.db.core);
          const source = (await this.db.sessions()).find(s => s.id === result.source_id);
          if (generation !== this.generation || ws.readyState !== WebSocket.OPEN) return;
          if (!source || source.stream !== 'business' || sequence(result.through_sequence) > sequence(source.ack)) throw Error('Capture state is not acknowledged');
          if (result.through_sequence === source.last) {
            this.captureStates.set(source.id, result); this.report('ready');
          }
        } else if (message.type === 'capture.profiles') {
          if (!this.ready || !this.capabilities.has(CAPTURE_PROFILES_CAPABILITY)) throw Error('Unnegotiated capture profiles');
          const result = parseCaptureProfiles(message);
          const source = (await this.db.sessions()).find(s => s.id === result.source_id);
          if (generation !== this.generation || ws.readyState !== WebSocket.OPEN) return;
          if (!source || source.stream !== 'business' || sequence(result.through_sequence) > sequence(source.ack)) throw Error('Profiles are not acknowledged');
          if (result.through_sequence === source.last) {
            this.captureProfiles.set(source.id, result); this.report('ready');
          }
        } else if (message.type === 'capture.recommendation') {
          if (!this.ready || !this.capabilities.has(RECOMMENDATIONS_CAPABILITY)) throw Error('Unnegotiated recommendation');
          const result = parseRecommendation(message);
          if (result.model?.search && !this.capabilities.has(SEARCH_CAPABILITY)) throw Error('Unnegotiated search');
          if (result.pr && !this.capabilities.has(PR_CAPABILITY)) throw Error('Unnegotiated Pr1.0 recommendation');
          const source = (await this.db.sessions()).find(s => s.id === result.source_id);
          if (generation !== this.generation || ws.readyState !== WebSocket.OPEN) return;
          if (!source || source.stream !== 'business' || sequence(result.through_sequence) > sequence(source.ack)) throw Error('Recommendation is not acknowledged');
          if (result.through_sequence === source.last) {
            this.recommendations.set(source.id, result); this.report('ready');
          }
        } else if (message.type === 'network.status') {
          if (!this.ready || !this.supportsStatus) throw Error('Unnegotiated server status');
          const result = parseStatus(message);
          const source = (await this.db.sessions()).find(s => s.id === result.source_id);
          if (generation !== this.generation || ws.readyState !== WebSocket.OPEN) return;
          if (!source || sequence(result.through_sequence) > sequence(source.ack)) throw Error('Status is not durably acknowledged');
          // Earlier batches are stale as soon as a newer local event exists.
          if (result.through_sequence === source.last) this.serverStatus = result;
        } else if (message.type === 'error') {
          throw Error('Backend rejected the stream; inspect server diagnostics before reconnecting');
        } else {
          throw Error('Unexpected backend message');
        }
      }).catch(() => {
        if (generation === this.generation && ws.readyState === WebSocket.OPEN) this.halt('error', 'Backend protocol/authentication error; records remain queued');
      });
    };
    ws.onerror = () => {}; // The close handler schedules retries; no duplicate retry loop.
    ws.onclose = event => {
      if (generation !== this.generation) return;
      this.generation++; // Retire asynchronous messages belonging to the closed connection.
      this.clearTimers(); this.ready = false; this.flight = undefined; this.serverStatus = undefined; this.captureStates.clear(); this.captureProfiles.clear(); this.recommendations.clear(); this.capabilities.clear();
      if (!this.enabled) return;
      if ([1008, 4401, 4403].includes(event.code)) {
        this.halt('error', 'Backend rejected authentication or protocol; reconnect with valid settings'); return;
      }
      this.report('retrying', 'Connection lost; unconfirmed records will be resent');
      const wait = Math.min(60000, 1000 * 2 ** Math.min(this.retries++, 6) * (0.8 + Math.random() * 0.4));
      this.retryTimer = setTimeout(() => this.open(), wait);
    };
  }
  pong: () => void = () => {};
  halt(state: string, error: string): void {
    this.stop(); this.report(state, error);
  }
  async pump(): Promise<void> {
    if (this.busy) { this.dirty = true; return; }
    if (!this.ready || this.flight) return;
    this.busy = true; this.dirty = false;
    const generation = this.generation;
    try {
      const streams: ('network' | 'business')[] = [];
      if (this.capabilities.has(CAPABILITY)) streams.push('network');
      if (this.capabilities.has(BUSINESS_CAPABILITY)) streams.push('business');
      let batch = await this.db.batch(streams, this.lastSource);
      if (generation !== this.generation || !this.ready || this.flight) return;
      let refresh = false;
      if (!batch && this.refreshPending) {
        this.refreshPending = false;
        if (streams.includes('business') && (this.capabilities.has(CAPTURE_STATE_CAPABILITY) || this.capabilities.has(CAPTURE_PROFILES_CAPABILITY))) {
          // An idle, fully acknowledged capture still needs durable state after reconnect.
          // Replay its last original event once; the server's existing dedupe owns counting.
          batch = await this.db.acknowledgedBusiness(); refresh = !!batch;
        }
      }
      if (!batch || generation !== this.generation || !this.ready || this.flight) return;
      const ws = this.socket;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const business = batch.source.stream === 'business';
      if (business && batch.source.state === 'active') this.refreshPending = false;
      const message = JSON.stringify({ schema: business ? BUSINESS_SCHEMA : SCHEMA,
        type: business ? 'capture.append' : 'network.append', source_id: batch.source.id, events: batch.events });
      if (new TextEncoder().encode(message).length > 256 * 1024) throw Error('Batch exceeds protocol limit');
      this.flight = { source: batch.source.id, through: batch.events.at(-1)!.sequence, ...(refresh ? { refresh: true } : {}) };
      this.lastSource = batch.source.id;
      ws.send(message);
      this.deadline = setTimeout(() => ws.close(), 10000);
    } finally {
      this.busy = false;
      if (this.dirty && this.ready && !this.flight) this.notify();
    }
  }
  notify(): void {
    void this.pump().catch(() => this.halt('error', 'Cannot read/send the durable queue; reconnect after checking storage'));
  }
}
