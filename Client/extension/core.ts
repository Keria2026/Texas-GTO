export interface Core {
  abi_version(): number;
  assess_sequence(last: bigint, next: bigint): number;
  validate_frame(opcode: number, bytes: number, limit: number): number;
  validate_ack(last: bigint, sent: bigint, ack: bigint): number;
  fits_budget(current: bigint, added: bigint, limit: bigint): number;
  memory: WebAssembly.Memory;
  prepare_input(len: number): number;
  process_wpk(): number;
  output_ptr(): number;
  clear_buffers(): void;
}
export interface BusinessEventInput { kind: string; table_id: string | null; hand_id: string | null; data: Record<string, unknown> }
export interface Reduction { ok: boolean; error?: string; events: BusinessEventInput[]; checkpoint: Record<string, unknown> }
export function wpkCall(core: Core, input: unknown): Reduction {
  const bytes = new TextEncoder().encode(JSON.stringify(input));
  if (bytes.length > 256 * 1024) throw Error('WPK input exceeds limit');
  const pointer = core.prepare_input(bytes.length);
  if (!pointer) throw Error('WASM input allocation failed');
  try {
    new Uint8Array(core.memory.buffer, pointer, bytes.length).set(bytes);
    const length = core.process_wpk();
    if (length > 256 * 1024) throw Error('WPK output exceeds limit');
    const result = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(core.memory.buffer, core.output_ptr(), length))) as Reduction;
    if (!result.ok) throw Error(result.error ?? 'WPK validation failed');
    return result;
  } finally { core.clear_buffers(); }
}
export async function loadCore(bytes: BufferSource): Promise<Core> {
  const { instance } = await WebAssembly.instantiate(bytes, {});
  const core = instance.exports as unknown as Core;
  if (core.abi_version() !== 3) throw Error('WASM ABI mismatch');
  return core;
}
