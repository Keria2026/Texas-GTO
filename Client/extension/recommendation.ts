import { sequence } from './model.ts';
import type { Session } from './model.ts';
import type { CaptureState } from './capture-state.ts';

export const RECOMMENDATIONS_CAPABILITY = 'capture_recommendations_v1';
export interface RecommendedAction {
  action: 'FOLD' | 'CHECK' | 'CALL' | 'BET' | 'RAISE' | 'ALL_IN';
  label: string; amount_delta: number; raise_to: number | null; probability: number;
}
export interface Recommendation {
  schema: 'tgto.capture-recommendation/0.1'; type: 'capture.recommendation';
  source_id: string; through_sequence: string; hand_id: string | null;
  status: 'ready' | 'unavailable'; reason: string; actions: RecommendedAction[];
  selected_action: number | null; issued_at_ms: number; expires_at_ms: number; elapsed_ms: number;
  model: null | { source: 'pluribus_blueprint'; version: string; iterations: number;
    rules_version: string; quality: 'finite_horizon_unvalidated'; horizon_hands: 2; opponent_adaptation: false };
  warnings: string[];
}
function fields(value: any, keys: string[]): asserts value is Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== keys.sort().join(',')) throw Error('Invalid recommendation fields');
}
function integer(value: unknown): value is number { return Number.isSafeInteger(value) && (value as number) >= 0; }
export function parseRecommendation(input: unknown): Recommendation {
  const m = input as Recommendation;
  fields(m, ['schema', 'type', 'source_id', 'through_sequence', 'hand_id', 'status', 'reason', 'actions',
    'selected_action', 'model', 'issued_at_ms', 'expires_at_ms', 'elapsed_ms', 'warnings']);
  if (m.schema !== 'tgto.capture-recommendation/0.1' || m.type !== 'capture.recommendation'
    || typeof m.source_id !== 'string' || !/^[a-zA-Z0-9-]{1,80}$/.test(m.source_id)
    || !['ready', 'unavailable'].includes(m.status)
    || typeof m.reason !== 'string' || !/^[a-z_]{1,80}$/.test(m.reason)) throw Error('Invalid recommendation envelope');
  sequence(m.through_sequence);
  if (m.hand_id !== null) {
    if (typeof m.hand_id !== 'string' || !/^table-[1-9][0-9]*:[1-9][0-9]*$/.test(m.hand_id)) throw Error('Invalid recommendation hand');
    const [table, hand] = m.hand_id.split(':'); sequence(table!.slice(6)); sequence(hand);
  }
  if (![m.issued_at_ms, m.expires_at_ms, m.elapsed_ms].every(integer)
    || m.expires_at_ms < m.issued_at_ms || m.expires_at_ms - m.issued_at_ms > 10_000
    || !Array.isArray(m.warnings) || m.warnings.length > 16
    || !m.warnings.every(s => typeof s === 'string' && /^[a-z_]{1,80}$/.test(s))
    || !Array.isArray(m.actions) || m.actions.length > 8) throw Error('Invalid recommendation bounds');
  if (m.status === 'unavailable') {
    if (m.actions.length || m.selected_action !== null || m.model !== null || m.expires_at_ms !== m.issued_at_ms)
      throw Error('Unavailable recommendation cannot carry actions');
  } else {
    if (!m.hand_id || !m.actions.length || !integer(m.selected_action) || m.selected_action >= m.actions.length
      || m.expires_at_ms === m.issued_at_ms || m.reason !== 'covered_blueprint') throw Error('Invalid ready recommendation');
    const model = m.model;
    fields(model, ['source', 'version', 'iterations', 'rules_version', 'quality', 'horizon_hands', 'opponent_adaptation']);
    if (model.source !== 'pluribus_blueprint' || typeof model.version !== 'string' || !/^[a-f0-9]{64}$/.test(model.version)
      || !integer(model.iterations) || model.iterations < 1 || typeof model.rules_version !== 'string'
      || !model.rules_version.startsWith('mushroom-context-v1:') || model.rules_version.length > 8192
      || model.quality !== 'finite_horizon_unvalidated' || model.horizon_hands !== 2 || model.opponent_adaptation !== false)
      throw Error('Invalid recommendation provenance');
    for (const a of m.actions) {
      fields(a, ['action', 'label', 'amount_delta', 'raise_to', 'probability']);
      if (!['FOLD', 'CHECK', 'CALL', 'BET', 'RAISE', 'ALL_IN'].includes(a.action)
        || !['fold', 'check', 'call', 'raise_min', 'raise_half_pot', 'raise_pot', 'all_in'].includes(a.label)
        || !integer(a.amount_delta) || !Number.isFinite(a.probability) || a.probability < 0 || a.probability > 1)
        throw Error('Invalid recommended action');
      if (['FOLD', 'CHECK', 'CALL'].includes(a.action)) {
        if (a.raise_to !== null || a.label !== a.action.toLowerCase()
          || (a.action === 'CALL' ? a.amount_delta === 0 : a.amount_delta !== 0)) throw Error('Invalid passive action amount');
      } else if (!integer(a.raise_to) || a.raise_to < a.amount_delta || a.amount_delta === 0
        || !['raise_min', 'raise_half_pot', 'raise_pot', 'all_in'].includes(a.label)) throw Error('Invalid wager total');
    }
    if (new Set(m.actions.map(a => a.label)).size !== m.actions.length
      || Math.abs(m.actions.reduce((sum, a) => sum + a.probability, 0) - 1) > 1e-9
      || m.actions[m.selected_action]!.probability <= 0) throw Error('Invalid mixed strategy');
  }
  return m;
}

export function currentRecommendation(value: Recommendation | undefined, session: Session | undefined,
  capture: CaptureState | undefined, now = Date.now()): Recommendation | undefined {
  if (!value || !session || session.state !== 'active' || value.source_id !== session.id
    || value.through_sequence !== session.last || sequence(value.through_sequence) > sequence(session.ack)
    || !capture || capture.source_id !== value.source_id || capture.through_sequence !== value.through_sequence
    || capture.state.hand_id !== value.hand_id) return undefined;
  if (value.status === 'ready') {
    const state = capture.state, turn = state.turn as { player_id?: string } | null;
    if (now >= value.expires_at_ms || value.issued_at_ms > now + 5000 || state.gap || state.closed
      || !state.history_complete || turn?.player_id !== 'hero') return undefined;
  }
  return value;
}
