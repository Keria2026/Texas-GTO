import type { ProbeReport, ProbeState } from './probe-model.ts';
export interface ProbeSession { source: string; count: number; bytes: number; state: ProbeState; shapes: number }
export interface ProbeRecord { source: string; index: number; context: string; captured_at: string; report: ProbeReport }
const req = <T>(r: IDBRequest<T>) => new Promise<T>((resolve, reject) => { r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); });
export class ProbeStore {
  db: IDBDatabase;
  constructor(db: IDBDatabase) { this.db = db; }
  static async open(name = 'tgto-wpk-probe-v1'): Promise<ProbeStore> {
    const r = indexedDB.open(name, 1);
    r.onupgradeneeded = () => {
      r.result.createObjectStore('sessions', { keyPath: 'source' });
      r.result.createObjectStore('reports', { keyPath: ['source', 'index'] }).createIndex('source', 'source');
    };
    const db = await req(r); db.onversionchange = () => db.close(); return new ProbeStore(db);
  }
  async transaction<T>(mode: IDBTransactionMode, work: (tx: IDBTransaction) => Promise<T>): Promise<T> {
    const tx = this.db.transaction(['sessions', 'reports'], mode, { durability: 'strict' });
    const done = new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onabort = () => reject(tx.error ?? Error('Probe transaction aborted')); tx.onerror = () => {}; });
    void done.catch(() => {});
    try { const result = await work(tx); await done; return result; }
    catch (error) { try { tx.abort(); } catch { /* Already completed. */ } await done.catch(() => {}); throw error; }
  }
  async append(source: string, context: string, report: ProbeReport): Promise<void> {
    await this.transaction('readwrite', async tx => {
      const sessions = tx.objectStore('sessions');
      const old = await req<ProbeSession | undefined>(sessions.get(source));
      const previous = old ?? { source, count: 0, bytes: 0, state: 'probing', shapes: 0 };
      const record: ProbeRecord = { source, index: previous.count + 1, context, captured_at: new Date().toISOString(), report };
      const bytes = new TextEncoder().encode(JSON.stringify(record)).length;
      if (previous.count >= 256 || previous.bytes + bytes > 2 * 1024 * 1024) throw Error('Probe archive limit reached');
      if (!old && await req(sessions.count()) >= 32) throw Error('Probe session limit reached');
      await req(tx.objectStore('reports').add(record));
      await req(sessions.put({ ...previous, count: record.index, bytes: previous.bytes + bytes,
        state: report.type === 'state' ? report.state : previous.state, shapes: previous.shapes + Number(report.type === 'shape') }));
    });
  }
  async session(source: string): Promise<ProbeSession | undefined> {
    return this.transaction('readonly', tx => req(tx.objectStore('sessions').get(source)));
  }
  async records(source: string): Promise<ProbeRecord[]> {
    return this.transaction('readonly', tx => req(tx.objectStore('reports').index('source').getAll(source)));
  }
}
