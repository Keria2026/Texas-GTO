export const PROBE_SCHEMA = 'tgto.wpk-probe/0.1';
export const probeStates = ['probing', 'observing', 'stopped', 'expired', 'entry_missing', 'entry_unsupported',
  'entry_replaced', 'sample_limit', 'inspection_failed', 'context_lost', 'interrupted'] as const;
export type ProbeState = typeof probeStates[number];
const messages = new Set(['WP_actionNotify', 'WP_actionHistoryNotify', 'WP_userOptNotify', 'WP_roundChangeNotify',
  'C_playLogNotify', 'C_updateRoomDetail', 'C_closeRoomNotify']);
const fieldTypes = new Set(['null', 'string', 'number', 'boolean', 'undefined', 'bigint', 'symbol', 'function',
  'object', 'array', 'circular', 'depth_limit', 'redacted', 'accessor']);
export type ProbeReport = { schema: typeof PROBE_SCHEMA; type: 'state'; state: ProbeState; entry?: 'global' | 'cocos_module' }
  | { schema: typeof PROBE_SCHEMA; type: 'shape'; message: string | null; phase: 'after_dispatch_microtask';
    threw: boolean; fields: { path: string; type: string }[]; truncated: boolean };
export function parseProbe(payload: string): ProbeReport {
  if (payload.length > 16384 || new TextEncoder().encode(payload).length > 16384) throw Error('Oversized probe report');
  const r = JSON.parse(payload) as Record<string, unknown>;
  if (!r || typeof r !== 'object' || r.schema !== PROBE_SCHEMA) throw Error('Invalid probe schema');
  if (r.type === 'state') {
    if (Object.keys(r).some(k => !['schema', 'type', 'state', 'entry'].includes(k))
      || !probeStates.includes(r.state as ProbeState) || (r.entry !== undefined && !['global', 'cocos_module'].includes(String(r.entry)))) throw Error('Invalid probe state');
  } else if (r.type === 'shape') {
    if (Object.keys(r).sort().join(',') !== 'fields,message,phase,schema,threw,truncated,type'
      || (r.message !== null && !messages.has(String(r.message))) || r.phase !== 'after_dispatch_microtask'
      || typeof r.threw !== 'boolean' || typeof r.truncated !== 'boolean' || !Array.isArray(r.fields) || r.fields.length > 96) throw Error('Invalid probe shape');
    for (const f of r.fields) {
      if (!f || typeof f !== 'object' || Object.keys(f).sort().join(',') !== 'path,type'
        || typeof f.path !== 'string' || !/^arg[0-3](?:\[\]|\.(?:[A-Za-z_$][A-Za-z0-9_$]{0,39}|\*)){0,5}$/.test(f.path)
        || !fieldTypes.has(f.type)) throw Error('Invalid probe field');
    }
  } else throw Error('Unknown probe report');
  return r as ProbeReport;
}
