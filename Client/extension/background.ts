import { currentRecommendation } from './recommendation.ts';
import { recoverCapture } from './capture-recovery.ts';
import { loadCore } from './core.ts';
import { CaptureDB } from './db.ts';
import { Collector, allowedPage } from './capture.ts';
import { BACKENDS, ResearchSocket } from './transport.ts';
import type { Target } from './capture.ts';
import { WpkAdapter } from './wpk.ts';

let transportState = 'disconnected';
let transportError: string | undefined;
let fault: string | undefined;
let waiting = 0, waitingBytes = 0, overflow = false;
let tasks: Promise<unknown> = Promise.resolve();
const methods = new Set(['Network.webSocketCreated', 'Network.webSocketFrameReceived', 'Network.webSocketFrameSent',
  'Network.webSocketClosed', 'Network.webSocketFrameError', 'Target.attachedToTarget', 'Target.detachedFromTarget', 'Page.frameNavigated',
  'Runtime.executionContextCreated', 'Runtime.executionContextDestroyed', 'Runtime.executionContextsCleared', 'Runtime.bindingCalled']);

function enqueue<T>(work: () => Promise<T>): Promise<T> {
  const next = tasks.then(work); tasks = next.catch(() => {}); return next;
}
async function failCapture(reason: string): Promise<void> {
  fault = reason;
  await chrome.storage.session.remove('captureIntent');
  await chrome.storage.local.set({ captureFault: reason }).catch(() => {});
  const app = await initialized;
  await app.collector.stop(reason).catch(async () => {
    // Even if a full database cannot accept a gap, the durable session remains unclean.
    const active = (await app.db.sessions()).find(s => s.state === 'active');
    if (active) await app.db.update(active.id, 'interrupted', reason).catch(() => {});
  });
  app.socket.notify();
}

// Register listeners synchronously, before IDB/WASM initialization.
chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!methods.has(method) || source.tabId === undefined || overflow) return;
  const data = (params ?? {}) as Record<string, unknown>;
  const response = data.response as { payloadData?: string } | undefined;
  const cost = ((response?.payloadData?.length ?? 0) + (typeof data.payload === 'string' ? data.payload.length : 0)) * 2 + 1024;
  if (waiting >= 128 || waitingBytes + cost > 8 * 1024 * 1024) {
    overflow = true;
    void enqueue(() => failCapture('Capture buffer overflow; coverage interrupted')).catch(() => {}); return;
  }
  waiting++; waitingBytes += cost;
  void enqueue(async () => {
    const app = await initialized;
    try { await app.collector.handle(source as Target, method, data); }
    catch { await failCapture('Frame processing or durable write failed; capture stopped'); }
    finally { waiting--; waitingBytes -= cost; }
  }).catch(() => {});
});
chrome.debugger.onDetach.addListener((source, reason) => {
  void enqueue(async () => {
    const app = await initialized;
    if (source.tabId === app.collector.active?.tabId) await failCapture(`Debugger detached: ${reason}`);
  }).catch(() => {});
});
chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name !== 'tgto-retry') return;
  void initialized.then(app => {
    if (app.socket.enabled && !app.socket.socket) app.socket.open();
    app.socket.notify();
  }).catch(() => {});
});
chrome.runtime.onMessage.addListener((message: unknown, sender, respond) => {
  if (sender.id !== chrome.runtime.id || sender.url?.split('?')[0] !== chrome.runtime.getURL('sidepanel.html')) return;
  void enqueue(async () => {
    const app = await initialized;
    if (!message || typeof message !== 'object') throw Error('Invalid command');
    const msg = message as Record<string, unknown>;
    if (msg.type === 'status') {
      const all = (await app.db.sessions()).sort((a, b) => b.createdAt.localeCompare(a.createdAt));
      const sessions = all.filter(s => s.stream !== 'business');
      const tabs = (await chrome.tabs.query({})).filter(t => t.url && allowedPage(t.url))
        .map(t => ({ id: t.id, label: `WPK · tab ${t.id}` }));
      const current = sessions[0];
      const business = all.find(s => s.stream === 'business' && s.parentSource === current?.id);
      const serverCapture = business && app.socket.captureStates.get(business.id);
      const profiles = business && app.socket.captureProfiles.get(business.id);
      return { recommendation: currentRecommendation(business && app.socket.recommendations.get(business.id), business, serverCapture), serverCapture: business?.state === 'active' && serverCapture?.through_sequence === business.last ? serverCapture : undefined, sessions, tabs, transportState, transportError, backendURL: app.socket.url, fault, wasm: 'ready',
        serverProfiles: business?.state === 'active' && profiles?.through_sequence === business.last ? profiles : undefined,
        business, businessStatus: business?.state === 'interrupted' ? 'interrupted' : business?.state === 'stopped' ? 'stopped' : app.business.status,
        businessPending: String(all.filter(s => s.stream === 'business').reduce((n, s) => n + BigInt(s.last) - BigInt(s.ack), 0n)),
        backendCapabilities: [...app.socket.capabilities],
        businessRecent: business ? await app.db.recent(business.id) : [],
        recent: current ? await app.db.recent(current.id) : [] };
    }
    if (msg.type === 'start') {
      if (typeof msg.tabId !== 'number' || typeof msg.raw !== 'boolean') throw Error('Invalid capture settings');
      const tab = await chrome.tabs.get(msg.tabId);
      await app.collector.start(msg.tabId, tab.url ?? '', msg.raw);
      await chrome.storage.session.set({ captureIntent: { source: app.collector.active!.id, tabId: msg.tabId, raw: msg.raw } });
      overflow = false; fault = undefined; await chrome.storage.local.remove('captureFault');
    } else if (msg.type === 'stop') {
      await chrome.storage.session.remove('captureIntent');
      await app.collector.stop();
    } else if (msg.type === 'connect') {
      if (typeof msg.url !== 'string' || (msg.token !== undefined && typeof msg.token !== 'string') || !BACKENDS.includes(msg.url)) throw Error('Invalid backend settings');
      const token = typeof msg.token === 'string' ? msg.token : '';
      app.socket.connect(msg.url, token);
      await chrome.storage.session.set({ backendEnabled: true, localAutoConnect: true, backendURL: msg.url, researchToken: token });
    } else if (msg.type === 'disconnect') {
      app.socket.stop(); await chrome.storage.session.set({ localAutoConnect: false });
      await chrome.storage.session.remove(['backendEnabled', 'researchToken']);
    } else if (msg.type === 'export') {
      if (typeof msg.source !== 'string' || typeof msg.raw !== 'boolean') throw Error('Invalid export source');
      const records = await app.db.records(msg.source);
      return records.map(r => msg.raw ? r : r.record);
    } else { throw Error('Unknown command'); }
    app.socket.notify(); return { ok: true };
  }).then(value => respond({ ok: true, value }), error => respond({ ok: false, error: error instanceof Error ? error.message : 'Operation failed' }));
  return true;
});
chrome.runtime.onInstalled.addListener(() => { void chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }); });

const initialized = (async () => {
  await chrome.storage.session.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' });
  const result = await fetch(chrome.runtime.getURL('capture_core.wasm'));
  if (!result.ok) throw Error('Cannot load packaged WASM');
  const core = await loadCore(await result.arrayBuffer());
  const db = await CaptureDB.open(core);
  const socket = new ResearchSocket(db, (state, error) => {
    transportState = state; transportError = error;
    void chrome.runtime.sendMessage({ type: 'state_changed' }).catch(() => {});
    if (state === 'error' || state === 'unsupported') void chrome.storage.session.set({ backendEnabled: false });
  });
  const business = new WpkAdapter(chrome.debugger, db, core, () => {
    socket.notify(); void chrome.runtime.sendMessage({ type: 'state_changed' }).catch(() => {});
  });
  const collector = new Collector(chrome.debugger, db, core, () => socket.notify(), business);
  const interrupted = (await db.sessions()).filter(s => s.state === 'active');
  for (const session of interrupted.sort((a, b) => Number(a.stream === 'business') - Number(b.stream === 'business'))) {
    if (session.state !== 'active') continue;
    if (session.stream === 'business') {
      await db.appendBusiness(session.id, [{ kind: 'capture_gap', table_id: null, hand_id: null,
        data: { reason: 'worker_restart: continuous business coverage unverified' } }],
      session.checkpoint ? { ...session.checkpoint, history_complete: false, turn: null } : undefined).catch(() => {});
    } else await db.append(session.id, 'capture_gap', { reason: 'worker_restart: continuous coverage unverified' }).catch(() => {});
    await db.update(session.id, 'interrupted', 'worker_restart').catch(() => { fault = 'Storage recovery write failed; exported records may contain an unclosed epoch'; });
    if (session.stream !== 'business') {
      await chrome.debugger.sendCommand({ tabId: session.tabId }, 'Runtime.evaluate', {
        expression: 'globalThis.__tgtoWpkBridge?.stop()',
      }).catch(() => {});
      await chrome.debugger.detach({ tabId: session.tabId }).catch(() => {});
    }
  }
  fault ??= (await chrome.storage.local.get('captureFault')).captureFault as string | undefined;
  await chrome.alarms.create('tgto-retry', { periodInMinutes: 0.5 });
  const saved = await chrome.storage.session.get(['backendEnabled', 'backendURL', 'researchToken', 'localAutoConnect']);
  const local = BACKENDS.find(url => url.startsWith('ws://127.0.0.1:'));
  if (local && saved.localAutoConnect !== false) {
    await chrome.storage.session.remove('researchToken');
    socket.connect(local);
  } else if (!local && saved.backendEnabled && typeof saved.backendURL === 'string' && typeof saved.researchToken === 'string') {
    socket.connect(saved.backendURL, saved.researchToken);
  }
  return { db, collector, socket, business, interrupted };
})();
void initialized.catch(() => { fault = 'Storage/WASM initialization failed; reopen the extension after checking browser errors'; });
// Drain old debugger callbacks and user stop commands before attaching a new capture epoch.
void initialized.then(app => enqueue(async () => {
  if (fault || app.collector.active) return;
  const { captureIntent } = await chrome.storage.session.get('captureIntent');
  const restored = await recoverCapture(captureIntent, app.interrupted, id => chrome.tabs.get(id),
    (id, url, raw) => app.collector.start(id, url, raw));
  if (restored) {
    const active = app.collector.active!;
    await chrome.storage.session.set({ captureIntent: { source: active.id, tabId: active.tabId, raw: active.raw } });
  } else await chrome.storage.session.remove('captureIntent');
})).catch(() => { fault = '采集自动恢复失败，请重新开始采集。'; });
