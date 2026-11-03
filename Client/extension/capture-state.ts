import { wpkCall } from './core.ts';
import type { Core } from './core.ts';
import { sequence } from './model.ts';
export const CAPTURE_STATE_CAPABILITY = 'capture_state_v1';
export interface CaptureState {
  schema: 'tgto.capture-state/0.1'; type: 'capture.state'; source_id: string; through_sequence: string;
  projection_version: 'wpk-observation/1'; state: Record<string, unknown>;
  analysis: { status: 'unsupported'; reason: 'unverified_rules_and_strategy_coverage'; actions: []; missing_fields: string[] };
}
function object(value: unknown, keys: string[]): Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== keys.sort().join(',')) throw Error('Invalid capture state fields');
  return value as Record<string, any>;
}
export function parseCaptureState(input: unknown, core: Core): CaptureState {
  const m = object(input, ['schema', 'type', 'source_id', 'through_sequence', 'projection_version', 'state', 'analysis']);
  if (m.schema !== 'tgto.capture-state/0.1' || m.type !== 'capture.state' || m.projection_version !== 'wpk-observation/1'
    || typeof m.source_id !== 'string' || !/^[a-zA-Z0-9-]{1,80}$/.test(m.source_id)) throw Error('Invalid capture state envelope');
  sequence(m.through_sequence);
  const s = object(m.state, ['table_id', 'hand_id', 'street', 'hero_cards', 'board', 'players', 'actions', 'turn',
    'history_complete', 'closed', 'rules_version', 'gap', 'settlements', 'pending_settlements']);
  for (const k of ['history_complete', 'closed', 'gap']) if (typeof s[k] !== 'boolean') throw Error('Invalid capture flags');
  if (s.rules_version !== 'wpk-observed-unverified-v1' || !['unknown', 'preflop', 'flop', 'turn', 'river', 'showdown', 'between_hands'].includes(s.street)) throw Error('Unknown observed rules/street');
  if ((s.table_id === null) !== (s.hand_id === null)) throw Error('Incomplete hand scope');
  const validate = (kind: string, data: unknown, table = s.table_id, hand = s.hand_id) => wpkCall(core, { op: 'validate', record: {
    schema: 'tgto.capture/0.1', type: 'business_event', adapter_version: 'wpk/1', source_id: m.source_id,
    sequence: m.through_sequence, captured_at: new Date().toISOString(), kind,
    // Empty initial projections still validate their typed fields using a local validation scope.
    table_id: table ?? 'table-1', hand_id: hand ?? 'table-1:1', data,
  } });
  validate('hero_cards', { cards: s.hero_cards }); validate('board_updated', { cards: s.board });
  validate('players_updated', { players: s.players }); validate('action_requested', { turn: s.turn });
  for (const [key, max] of [['actions', 256], ['settlements', 8], ['pending_settlements', 16]] as const)
    if (!Array.isArray(s[key]) || s[key].length > max) throw Error('Capture state exceeds history bound');
  for (const a of s.actions) validate('action_confirmed', a);
  for (const { hand_id, ...data } of s.settlements) validate('settlement', data, data.table_id, hand_id);
  for (const { hand_id, ...data } of s.pending_settlements) {
    if (typeof hand_id !== 'string') throw Error('Invalid pending hand');
    validate('settlement_pending', data, hand_id.split(':')[0], hand_id);
  }
  const a = object(m.analysis, ['status', 'reason', 'actions', 'missing_fields']);
  if (a.status !== 'unsupported' || a.reason !== 'unverified_rules_and_strategy_coverage'
    || !Array.isArray(a.actions) || a.actions.length || !Array.isArray(a.missing_fields)
    || a.missing_fields.length > 32 || !a.missing_fields.every((v: unknown) => typeof v === 'string' && v.length <= 80)) throw Error('Invalid coverage result');
  return m as CaptureState;
}
