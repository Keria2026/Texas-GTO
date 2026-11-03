import { sequence } from './model.ts';
export const STATUS_CAPABILITY = 'network_status_v1';
export interface NetworkStatus {
  type: 'network.status'; schema: 'tgto.network-status/0.1'; source_id: string;
  through_sequence: string; status: 'blocked'; reason: 'business_events_unavailable';
  actions: never[]; missing_fields: string[];
}
export function parseStatus(value: Record<string, unknown>): NetworkStatus {
  if (Object.keys(value).sort().join(',') !== 'actions,missing_fields,reason,schema,source_id,status,through_sequence,type'
    || value.type !== 'network.status' || value.schema !== 'tgto.network-status/0.1'
    || typeof value.source_id !== 'string' || !/^[a-zA-Z0-9-]{1,80}$/.test(value.source_id)
    || value.status !== 'blocked' || value.reason !== 'business_events_unavailable'
    || !Array.isArray(value.actions) || value.actions.length !== 0
    || !Array.isArray(value.missing_fields) || value.missing_fields.length > 16
    || !value.missing_fields.every(field => typeof field === 'string' && /^[a-z_]{1,64}$/.test(field))
    || sequence(value.through_sequence) === 0n) throw Error('Invalid server status');
  return value as unknown as NetworkStatus;
}
