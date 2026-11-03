import type { Core } from './core.ts';
import type { CaptureDB } from './db.ts';
import { MAX_FRAME } from './model.ts';
import type { Session } from './model.ts';
import type { WpkAdapter } from './wpk.ts';

declare const __FIXTURE__: boolean;
export type Target = { tabId: number; sessionId?: string };
export interface DebuggerAPI {
  attach(target: Target, version: string): Promise<void>;
  detach(target: Target): Promise<void>;
  sendCommand(target: Target, method: string, params?: object): Promise<unknown>;
}
export function allowedPage(raw: string): boolean {
  try {
    const url = new URL(raw.startsWith('blob:') ? raw.slice(5) : raw);
    if (url.username || url.password) return false;
    return (url.protocol === 'https:' && url.hostname === 'h5.wpk.com' && url.port === '')
      || (typeof __FIXTURE__ !== 'undefined' && __FIXTURE__ && url.origin === 'http://127.0.0.1:8767');
  } catch { return false; }
}
export function endpoint(raw: string): string | null {
  try {
    const url = new URL(raw);
    if (url.username || url.password) return null;
    const game = url.protocol === 'wss:' && url.host === 'game.wpk111.com:11111'
      && url.pathname === '/wepoker/game4/battle/echo';
    const lobby = url.protocol === 'wss:' && url.host === 'a92smtp.depucloud.cn:18002' && url.pathname === '/';
    const fixture = typeof __FIXTURE__ !== 'undefined' && __FIXTURE__ && url.origin === 'ws://127.0.0.1:8767';
    return game || lobby || fixture ? `${url.origin}${url.pathname}` : null;
  } catch { return null; }
}
type Connection = { id: string; endpoint: string };
export class Collector {
  api: DebuggerAPI;
  db: CaptureDB;
  core: Core;
  changed: () => void;
  active?: Session;
  connections = new Map<string, Connection>();
  children = new Set<string>();
  business?: WpkAdapter;
  constructor(api: DebuggerAPI, db: CaptureDB, core: Core, changed: () => void, business?: WpkAdapter) {
    this.api = api; this.db = db; this.core = core; this.changed = changed; this.business = business;
  }
  async enable(target: Target): Promise<void> {
    await this.api.sendCommand(target, 'Network.enable', { maxTotalBufferSize: 4 * MAX_FRAME });
    await this.api.sendCommand(target, 'Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: true,
      flatten: true, filter: [{ type: 'iframe', exclude: false }, { type: 'worker', exclude: false },
        { type: 'shared_worker', exclude: false }, { exclude: true }] });
  }
  async start(tabId: number, url: string, raw: boolean): Promise<void> {
    if (this.active) throw Error('Stop the current capture first');
    if (!Number.isSafeInteger(tabId) || !allowedPage(url)) throw Error('Select an allowed WPK tab');
    await this.api.attach({ tabId }, '1.3');
    try {
      this.active = await this.db.create(tabId, raw);
      await this.db.append(this.active.id, 'capture_gap', { reason: 'attached_mid_stream: messages before attachment are unavailable' });
      await this.enable({ tabId });
      await this.api.sendCommand({ tabId }, 'Page.enable');
      await this.business?.start(this.active);
      this.changed();
    } catch (error) {
      if (this.active) await this.stop('initialization_failed').catch(() => {});
      else await this.api.detach({ tabId }).catch(() => {});
      throw error;
    }
  }
  key(target: Target, requestId: unknown): string {
    return `${target.sessionId ?? 'root'}:${String(requestId)}`;
  }
  async handle(target: Target, method: string, params: Record<string, unknown>): Promise<void> {
    const active = this.active;
    if (!active || target.tabId !== active.tabId) return;
    if (target.sessionId && !this.children.has(target.sessionId)) return;
    if (method.startsWith('Runtime.')) { await this.business?.handle(target, method, params); return; }
    if (method === 'Target.attachedToTarget') {
      const info = params.targetInfo as { url?: string; type?: string } | undefined;
      const child = { tabId: target.tabId, sessionId: String(params.sessionId) };
      try {
        if (info?.url && allowedPage(info.url) && ['iframe', 'worker', 'shared_worker'].includes(info.type ?? '')) {
          this.children.add(child.sessionId);
          await this.enable(child);
        }
      } finally {
        // Every paused child is resumed even if filtering/setup fails.
        await this.api.sendCommand(child, 'Runtime.runIfWaitingForDebugger');
        if (!this.children.has(child.sessionId)) await this.api.sendCommand(target, 'Target.detachFromTarget', { sessionId: child.sessionId }).catch(() => {});
      }
      return;
    }
    if (method === 'Target.detachedFromTarget') {
      const id = String(params.sessionId);
      if (this.children.delete(id)) {
        for (const key of this.connections.keys()) if (key.startsWith(`${id}:`)) this.connections.delete(key);
        await this.db.append(active.id, 'capture_gap', { reason: 'child_target_detached' }); this.changed();
      }
      return;
    }
    if (method === 'Page.frameNavigated' && !target.sessionId) {
      const frame = params.frame as { parentId?: string } | undefined;
      if (frame && !frame.parentId) await this.stop('page_navigation');
      return;
    }
    const key = this.key(target, params.requestId);
    if (method === 'Network.webSocketCreated') {
      const url = typeof params.url === 'string' ? endpoint(params.url) : null;
      if (!url) return;
      const connection = { id: crypto.randomUUID(), endpoint: url };
      this.connections.set(key, connection);
      await this.db.append(active.id, 'socket_open', { connection_id: connection.id, endpoint: url }); this.changed(); return;
    }
    const connection = this.connections.get(key);
    if (!connection) return; // Existing/unknown endpoints cannot bypass the allowlist.
    if (method === 'Network.webSocketClosed' || method === 'Network.webSocketFrameError') {
      const kind = method.endsWith('Closed') ? 'socket_closed' : 'socket_error';
      await this.db.append(active.id, kind, { connection_id: connection.id, endpoint: connection.endpoint,
        ...(kind === 'socket_error' ? { reason: 'websocket_frame_error' } : {}) });
      if (kind === 'socket_closed') this.connections.delete(key);
      this.changed(); return;
    }
    if (!['Network.webSocketFrameReceived', 'Network.webSocketFrameSent'].includes(method)) return;
    const frame = params.response as { opcode?: unknown; payloadData?: unknown } | undefined;
    if (!frame || !Number.isInteger(frame.opcode) || (frame.opcode as number) < 0 || (frame.opcode as number) > 255 || typeof frame.payloadData !== 'string'
      || frame.payloadData.length > 2 * MAX_FRAME) throw Error('Malformed or oversized CDP frame');
    const opcode = frame.opcode as number;
    const bytes = opcode === 1 ? new TextEncoder().encode(frame.payloadData)
      : Uint8Array.from(atob(frame.payloadData), char => char.charCodeAt(0));
    if (this.core.validate_frame(opcode, bytes.length, MAX_FRAME)) throw Error('WASM rejected WebSocket frame');
    const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
    const sha256 = Array.from(hash, b => b.toString(16).padStart(2, '0')).join('');
    await this.db.append(active.id, 'network_message', { connection_id: connection.id, endpoint: connection.endpoint,
      direction: method.endsWith('Received') ? 'received' : 'sent', opcode, byte_length: bytes.length, sha256,
      ...(typeof params.timestamp === 'number' && Number.isFinite(params.timestamp) ? { monotonic_time: params.timestamp } : {}) },
    { encoding: opcode === 1 ? 'utf8' : 'base64', value: frame.payloadData });
    this.changed();
  }
  async stop(reason?: string): Promise<void> {
    const active = this.active; this.active = undefined;
    if (!active) return;
    let failure: unknown;
    try {
      if (reason) await this.db.append(active.id, 'capture_gap', { reason }).catch(error => { failure = error; });
      await this.db.update(active.id, reason ? 'interrupted' : 'stopped', reason).catch(error => { failure = error; });
    } finally {
      await this.business?.stop(reason).catch(() => {});
      this.connections.clear(); this.children.clear();
      await this.api.detach({ tabId: active.tabId }).catch(() => {});
      this.changed();
    }
    if (failure) throw failure;
  }
}
