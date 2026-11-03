import type { Core } from './core.ts';
import { wpkCall, WpkValidationError } from './core.ts';
import type { CaptureDB } from './db.ts';
import type { DebuggerAPI, Target } from './capture.ts';
import type { Session } from './model.ts';
import { installWpkPage } from './wpk-page.ts';

export class WpkAdapter {
  api: DebuggerAPI; db: CaptureDB; core: Core; changed: () => void;
  source?: Session; binding = ''; frame = ''; context?: number;
  checkpoint?: Record<string, unknown>; status = 'inactive';
  waitingHand = false;
  constructor(api: DebuggerAPI, db: CaptureDB, core: Core, changed: () => void) {
    this.api = api; this.db = db; this.core = core; this.changed = changed;
  }
  async start(network: Session): Promise<void> {
    this.source = await this.db.create(network.tabId, false, network.id);
    this.checkpoint = undefined; this.context = undefined; this.status = 'waiting_client'; this.waitingHand = false;
    this.binding = `__tgtoWpk_${crypto.randomUUID().replaceAll('-', '')}`;
    await this.db.appendBusiness(this.source.id, [{ kind: 'capture_gap', table_id: null, hand_id: null,
      data: { reason: 'attached_mid_hand: business history before attachment is unavailable' } }]);
    const target = { tabId: network.tabId };
    try {
      const tree = await this.api.sendCommand(target, 'Page.getFrameTree') as { frameTree: { frame: { id: string } } };
      this.frame = tree.frameTree.frame.id;
      await this.api.sendCommand(target, 'Runtime.enable');
      await this.api.sendCommand(target, 'Runtime.addBinding', { name: this.binding });
      const result = await this.api.sendCommand(target, 'Runtime.evaluate', {
        expression: `(${installWpkPage.toString()})(${JSON.stringify(this.binding)})`, returnByValue: true,
      }) as { exceptionDetails?: unknown };
      if (result.exceptionDetails) throw Error('Page installation failed');
    } catch { await this.stop('adapter_installation_failed'); }
    this.changed();
  }
  async handle(target: Target, method: string, params: Record<string, unknown>): Promise<void> {
    const source = this.source;
    if (!source || target.tabId !== source.tabId || target.sessionId) return;
    if (method === 'Runtime.executionContextCreated') {
      const c = params.context as { id: number; auxData?: { isDefault?: boolean; frameId?: string } };
      if (c.auxData?.isDefault && c.auxData.frameId === this.frame) this.context = c.id;
      return;
    }
    if (method === 'Runtime.executionContextsCleared'
      || (method === 'Runtime.executionContextDestroyed' && params.executionContextId === this.context)) {
      if (this.context !== undefined) await this.stop('business_context_replaced'); return;
    }
    if (method !== 'Runtime.bindingCalled' || params.name !== this.binding || params.executionContextId !== this.context) return;
    try {
      if (typeof params.payload !== 'string' || params.payload.length > 256 * 1024) throw Error('Invalid binding frame');
      const input = JSON.parse(params.payload) as { status?: unknown; dto?: unknown };
      if (input.status !== undefined) {
        if (!['waiting_client', 'waiting_seat', 'ready', 'adapter_error'].includes(String(input.status))) throw Error('Invalid adapter status');
        if (input.status === 'adapter_error') { await this.stop('page_adapter_error_or_identity_change'); return; }
        if (input.status === 'waiting_seat' && this.checkpoint) await this.waitForHand('own_seat_temporarily_unavailable');
        this.status = input.status === 'ready' && this.waitingHand ? 'waiting_hand' : String(input.status); this.changed(); return;
      }
      if (this.waitingHand && (input.dto as { event?: unknown } | undefined)?.event !== 'WP_bankerChangeNotify') return;
      const reduction = wpkCall(this.core, { op: 'reduce', message: input.dto, checkpoint: this.checkpoint ?? null });
      await this.db.appendBusiness(source.id, reduction.events, reduction.checkpoint);
      // Publish only after the checkpoint, events and outbox commit atomically.
      this.checkpoint = reduction.checkpoint; this.waitingHand = false; this.status = 'ready'; this.changed();
    } catch (error) {
      if (error instanceof WpkValidationError) {
        const category = /settlement|final log|final board/i.test(error.message) ? 'settlement_conflict'
          : /cards|board/i.test(error.message) ? 'cards_conflict'
            : /action/i.test(error.message) ? 'action_conflict' : 'invalid_business_event';
        await this.waitForHand(`business_validation_failed:${category}`).catch(() => this.stop('business_storage_failed'));
      } else await this.stop('business_validation_or_storage_failed');
    }
  }
  async waitForHand(reason: string): Promise<void> {
    if (!this.source || this.waitingHand) return;
    const checkpoint = this.checkpoint ? { ...this.checkpoint, history_complete: false, turn: null } : undefined;
    await this.db.appendBusiness(this.source.id, [{ kind: 'capture_gap', table_id: null, hand_id: null, data: { reason } }], checkpoint);
    this.checkpoint = checkpoint; this.waitingHand = true; this.status = 'waiting_hand'; this.changed();
  }
  async stop(reason?: string): Promise<void> {
    const source = this.source; this.source = undefined; this.context = undefined;
    this.status = reason ? 'interrupted' : 'inactive';
    if (!source) return;
    try {
      if (reason) await this.db.appendBusiness(source.id, [{ kind: 'capture_gap', table_id: null, hand_id: null, data: { reason } }],
        this.checkpoint ? { ...this.checkpoint, history_complete: false, turn: null } : undefined).catch(() => {});
    } finally {
      await this.db.update(source.id, reason ? 'interrupted' : 'stopped', reason).catch(() => {});
      const target = { tabId: source.tabId };
      await this.api.sendCommand(target, 'Runtime.evaluate', { expression:
        `if (globalThis.__tgtoWpkBridge?.binding === ${JSON.stringify(this.binding)}) globalThis.__tgtoWpkBridge.stop()` }).catch(() => {});
      await this.api.sendCommand(target, 'Runtime.removeBinding', { name: this.binding }).catch(() => {});
      this.changed();
    }
  }
}
