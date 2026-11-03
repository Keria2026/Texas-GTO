export const SCHEMA = 'tgto.network/0.1';
export const CAPABILITY = 'network_events_v1';
export const BUSINESS_SCHEMA = 'tgto.capture/0.1';
export const BUSINESS_CAPABILITY = 'capture_events_v1';
export const MAX_FRAME = 1024 * 1024;
export const MAX_ARCHIVE = 64 * 1024 * 1024;
export const MAX_U64 = 18446744073709551615n;
export type Kind = 'network_message' | 'capture_gap' | 'socket_open' | 'socket_closed' | 'socket_error';
export interface NetworkData {
  connection_id?: string;
  endpoint?: string;
  direction?: 'received' | 'sent';
  opcode?: number;
  byte_length?: number;
  sha256?: string;
  monotonic_time?: number;
  reason?: string;
}
export interface RecordEnvelope {
  schema: typeof SCHEMA;
  type: Kind;
  source_id: string;
  sequence: string;
  captured_at: string;
  data: NetworkData;
}
export interface StoredRecord {
  key: [string, string];
  source: string;
  record: CaptureRecord;
  bytes: number;
  // Local opt-in diagnostics never enter network.append or status messages.
  payload?: { encoding: 'utf8' | 'base64'; value: string };
}
export interface BusinessRecord {
  schema: typeof BUSINESS_SCHEMA; type: 'business_event'; adapter_version: 'wpk/1';
  source_id: string; sequence: string; captured_at: string; kind: string;
  table_id: string | null; hand_id: string | null;
  data: Record<string, unknown> & { reason?: string };
}
export type CaptureRecord = RecordEnvelope | BusinessRecord;
export interface Session {
  id: string;
  tabId: number;
  createdAt: string;
  state: 'active' | 'stopped' | 'interrupted';
  last: string;
  ack: string;
  totalBytes: number;
  pendingBytes: number;
  raw: boolean;
  error?: string;
  stream?: 'network' | 'business';
  parentSource?: string;
  checkpoint?: Record<string, unknown>;
}
export function sequence(value: unknown): bigint {
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]{0,19})$/.test(value)) throw Error('Invalid sequence');
  const result = BigInt(value);
  if (result > MAX_U64) throw Error('Sequence exceeds u64');
  return result;
}
export function recordKey(source: string, seq: string): [string, string] {
  sequence(seq);
  return [source, seq.padStart(20, '0')];
}
