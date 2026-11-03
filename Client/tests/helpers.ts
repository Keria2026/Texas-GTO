import 'fake-indexeddb/auto';
import { readFile } from 'node:fs/promises';
import { loadCore } from '../extension/core.ts';
import { CaptureDB } from '../extension/db.ts';
export const core = await loadCore(await readFile(new URL('../target/wasm32-unknown-unknown/release/tgto_capture_core.wasm', import.meta.url)));
export async function database(limit?: number) { return CaptureDB.open(core, crypto.randomUUID(), limit); }
export async function until(check: () => Promise<boolean> | boolean, timeout = 8000) {
  const end = Date.now() + timeout;
  while (!await check()) {
    if (Date.now() > end) throw Error('Timed out waiting for condition');
    await new Promise(resolve => setTimeout(resolve, 30));
  }
}
