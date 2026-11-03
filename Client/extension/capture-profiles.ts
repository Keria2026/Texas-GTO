import { sequence } from './model.ts';

export const CAPTURE_PROFILES_CAPABILITY = 'capture_profiles_v1';
export const PROFILE_ACTIONS = ['FOLD', 'CHECK', 'CALL', 'BET', 'RAISE', 'ALL_IN'] as const;
type Action = typeof PROFILE_ACTIONS[number];
type Distribution = Record<Action, number>;
export interface ProfileContext {
  street: 'preflop' | 'flop' | 'turn' | 'river' | 'unknown';
  quality: 'complete_observed_history' | 'incomplete_history' | 'unknown_street';
  observations: number; counts: Distribution; frequencies: Distribution;
  posterior_probabilities: Distribution; prior_per_action: 1;
}
export interface CaptureProfiles {
  schema: 'tgto.capture-profiles/0.1'; type: 'capture.profiles'; source_id: string; through_sequence: string;
  identity_scope: 'origin_source'; quality: 'observed_behavior_learning';
  players: { player_id: string; action_observations: number; contexts: ProfileContext[] }[];
  warnings: string[];
}
function object(value: unknown, keys: readonly string[]): Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).sort().join(',') !== [...keys].sort().join(',')) throw Error('Invalid profile fields');
  return value as Record<string, any>;
}
function count(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0;
}
function close(actual: unknown, expected: number): boolean {
  return typeof actual === 'number' && Number.isFinite(actual) && Math.abs(actual - expected) <= 1e-9;
}
export function parseCaptureProfiles(input: unknown): CaptureProfiles {
  const m = object(input, ['schema', 'type', 'source_id', 'through_sequence', 'identity_scope', 'quality', 'players', 'warnings']);
  if (m.schema !== 'tgto.capture-profiles/0.1' || m.type !== 'capture.profiles'
    || m.identity_scope !== 'origin_source' || m.quality !== 'observed_behavior_learning'
    || typeof m.source_id !== 'string' || !/^[a-zA-Z0-9-]{1,80}$/.test(m.source_id)
    || sequence(m.through_sequence) === 0n) throw Error('Invalid profile envelope');
  if (!Array.isArray(m.players) || m.players.length > 8 || !Array.isArray(m.warnings)
    || m.warnings.length > 32 || !m.warnings.every((v: unknown) => typeof v === 'string' && v.length <= 512)) throw Error('Invalid profile collection');
  const players = new Set<string>();
  for (const value of m.players) {
    const p = object(value, ['player_id', 'action_observations', 'contexts']);
    if (typeof p.player_id !== 'string' || (p.player_id !== 'hero'
      && (!/^player-[1-9][0-9]{0,19}$/.test(p.player_id) || sequence(p.player_id.slice(7)) === 0n))
      || players.has(p.player_id) || !count(p.action_observations)
      || !Array.isArray(p.contexts) || p.contexts.length > 15) throw Error('Invalid profile player');
    players.add(p.player_id);
    const contexts = new Set<string>(); let total = 0;
    for (const entry of p.contexts) {
      const c = object(entry, ['street', 'quality', 'observations', 'counts', 'frequencies', 'posterior_probabilities', 'prior_per_action']);
      const key = `${c.street}:${c.quality}`;
      if (!['preflop', 'flop', 'turn', 'river', 'unknown'].includes(c.street)
        || !['complete_observed_history', 'incomplete_history', 'unknown_street'].includes(c.quality)
        || contexts.has(key) || !count(c.observations) || !c.observations || c.prior_per_action !== 1) throw Error('Invalid profile context');
      contexts.add(key);
      const counts = object(c.counts, PROFILE_ACTIONS);
      if (!PROFILE_ACTIONS.every(a => count(counts[a]))
        || PROFILE_ACTIONS.reduce((sum, a) => sum + counts[a], 0) !== c.observations) throw Error('Invalid action counts');
      for (const field of ['frequencies', 'posterior_probabilities']) {
        const probabilities = object(c[field], PROFILE_ACTIONS);
        if (!PROFILE_ACTIONS.every(a => typeof probabilities[a] === 'number' && probabilities[a] >= 0 && probabilities[a] <= 1
          && close(probabilities[a], field === 'frequencies' ? counts[a] / c.observations : (counts[a] + 1) / (c.observations + 6)))
          || !close(PROFILE_ACTIONS.reduce((sum, a) => sum + probabilities[a], 0), 1)) throw Error('Invalid action probabilities');
      }
      total += c.observations;
    }
    if (!Number.isSafeInteger(total) || total !== p.action_observations) throw Error('Invalid player observation total');
  }
  return m as CaptureProfiles;
}
