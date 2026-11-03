import { allowedPage } from '../capture.ts';
import type { DebuggerAPI, Target } from '../capture.ts';
import { installProbe } from './page-probe.js';
import { parseProbe, PROBE_SCHEMA } from './probe-model.ts';
import type { ProbeState } from './probe-model.ts';
import type { ProbeStore } from './probe-store.ts';
interface Context { target: Target; id: number; alias: string; binding: string; control: string }
export class WpkProbeBridge {
  api: DebuggerAPI;
  store: ProbeStore;
  source?: string;
  error?: string;
  contexts = new Map<string, Context>();
  targets = new Set<string>();
  constructor(api: DebuggerAPI, store: ProbeStore) { this.api = api; this.store = store; }
  targetKey(t: Target): string { return `${t.tabId}:${t.sessionId ?? 'root'}`; }
  contextKey(t: Target, id: number): string { return `${this.targetKey(t)}:${id}`; }
  async begin(source: string): Promise<void> {
    await this.stop(); this.source = source; this.error = undefined;
    try { await this.state('epoch', 'probing'); } catch { this.error = 'probe_storage_unavailable'; }
  }
  async state(context: string, state: ProbeState): Promise<void> {
    if (this.source) await this.store.append(this.source, context, { schema: PROBE_SCHEMA, type: 'state', state });
  }
  async enable(target: Target): Promise<void> {
    if (!this.source || this.error) return;
    this.targets.add(this.targetKey(target));
    try { await this.api.sendCommand(target, 'Runtime.enable'); }
    catch { this.error = 'probe_runtime_unavailable'; await this.state('runtime', 'entry_unsupported').catch(() => {}); }
  }
  async release(c: Context): Promise<void> {
    await this.api.sendCommand(c.target, 'Runtime.evaluate', {
      expression: `globalThis[${JSON.stringify(c.control)}]?.stop()`, contextId: c.id, returnByValue: true,
    }).catch(() => {});
    await this.api.sendCommand(c.target, 'Runtime.removeBinding', { name: c.binding }).catch(() => {});
  }
  async detached(target: Target): Promise<void> {
    this.targets.delete(this.targetKey(target));
    for (const [key, c] of this.contexts) {
      if (this.targetKey(c.target) !== this.targetKey(target)) continue;
      this.contexts.delete(key); await this.state(c.alias, 'context_lost').catch(() => {});
    }
  }
  async handle(target: Target, method: string, params: Record<string, unknown>): Promise<void> {
    if (!this.source || !this.targets.has(this.targetKey(target))) return;
    if (method === 'Runtime.executionContextCreated') {
      const context = params.context as { id?: unknown; origin?: unknown; auxData?: { isDefault?: unknown } } | undefined;
      if (!context || !Number.isSafeInteger(context.id) || (context.id as number) <= 0
        || typeof context.origin !== 'string' || !allowedPage(context.origin)
        || context.auxData?.isDefault !== true) return;
      const id = context.id as number, key = this.contextKey(target, id);
      if (this.contexts.has(key) || this.error) return;
      if (this.contexts.size >= 16) { this.error = 'probe_context_limit'; return; }
      const nonce = crypto.randomUUID().replaceAll('-', '');
      const c = { target, id, alias: `context-${nonce}`, binding: `__tgto_wpk_${nonce}`, control: `__tgto_wpk_control_${nonce}` };
      this.contexts.set(key, c);
      try {
        await this.api.sendCommand(target, 'Runtime.addBinding', { name: c.binding, executionContextId: id });
        const response = await this.api.sendCommand(target, 'Runtime.evaluate', {
          expression: `(${installProbe.toString()})(${JSON.stringify(c.binding)},${JSON.stringify(c.control)})`,
          contextId: id, returnByValue: true,
        }) as { exceptionDetails?: unknown; result?: { value?: { status?: unknown } } };
        if (response?.exceptionDetails || !['installed', 'already_installed'].includes(String(response?.result?.value?.status))) throw Error('Probe injection failed');
      } catch {
        this.contexts.delete(key); await this.release(c);
        await this.state(c.alias, 'entry_unsupported').catch(() => { this.error = 'probe_storage_unavailable'; });
      }
    } else if (method === 'Runtime.executionContextDestroyed') {
      if (!Number.isSafeInteger(params.executionContextId)) return;
      const key = this.contextKey(target, params.executionContextId as number), c = this.contexts.get(key);
      if (c) { this.contexts.delete(key); await this.state(c.alias, 'context_lost').catch(() => {}); }
    } else if (method === 'Runtime.executionContextsCleared') {
      await this.detached(target); this.targets.add(this.targetKey(target));
    } else if (method === 'Runtime.bindingCalled') {
      if (!Number.isSafeInteger(params.executionContextId)) return;
      const c = this.contexts.get(this.contextKey(target, params.executionContextId as number));
      if (!c || params.name !== c.binding) return;
      try {
        if (typeof params.payload !== 'string') throw Error('Invalid probe payload');
        await this.store.append(this.source, c.alias, parseProbe(params.payload));
      } catch {
        this.error = 'probe_report_rejected_or_storage_full'; this.contexts.delete(this.contextKey(target, c.id));
        await this.release(c); await this.state(c.alias, 'interrupted').catch(() => {});
      }
    }
  }
  async stop(): Promise<void> {
    if (!this.source) return;
    for (const c of this.contexts.values()) await this.release(c);
    this.contexts.clear(); this.targets.clear();
    await this.state('epoch', 'stopped').catch(() => {}); this.source = undefined;
  }
}
