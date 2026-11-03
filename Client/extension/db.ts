import { wpkCall } from './core.ts';
import type { Core, BusinessEventInput } from './core.ts';
import { MAX_ARCHIVE, MAX_U64, SCHEMA, BUSINESS_SCHEMA, recordKey, sequence } from './model.ts';
import type { Kind, NetworkData, RecordEnvelope, CaptureRecord, Session, StoredRecord } from './model.ts';

function request<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? Error('IndexedDB request failed'));
  });
}
async function transaction<T>(db: IDBDatabase, names: string[], mode: IDBTransactionMode,
  work: (tx: IDBTransaction) => Promise<T>): Promise<T> {
  const tx = db.transaction(names, mode, { durability: 'strict' });
  const done = new Promise<void>((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onabort = () => reject(tx.error ?? Error('IndexedDB transaction aborted'));
    tx.onerror = () => {}; // onabort owns the transaction error.
  });
  // A request may fail before work() unwinds; keep the transaction rejection observed.
  void done.catch(() => {});
  try {
    const value = await work(tx);
    await done;
    return value;
  } catch (error) {
    try { tx.abort(); } catch { /* Already committed/aborted. */ }
    await done.catch(() => {});
    throw error;
  }
}
export class CaptureDB {
  db: IDBDatabase;
  core: Core;
  limit: number;
  constructor(db: IDBDatabase, core: Core, limit = MAX_ARCHIVE) {
    this.db = db; this.core = core; this.limit = limit;
  }
  static async open(core: Core, name = 'tgto-capture-v1', limit = MAX_ARCHIVE): Promise<CaptureDB> {
    const req = indexedDB.open(name, 2);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains('settings')) req.result.createObjectStore('settings');
      if (req.result.objectStoreNames.contains('sessions')) return;
      req.result.createObjectStore('sessions', { keyPath: 'id' });
      for (const name of ['events', 'outbox']) {
        const store = req.result.createObjectStore(name, { keyPath: 'key' });
        store.createIndex('source', 'source');
      }
    };
    const db = await request(req);
    db.onversionchange = () => db.close();
    return new CaptureDB(db, core, limit);
  }
  async prepareBackend(url: string): Promise<void> {
    // ACKs belong to one receiver. Switching endpoints (or upgrading an unbound v1 DB)
    // replays metadata from the retained archive; local raw payloads stay local.
    await transaction(this.db, ['settings', 'sessions', 'events', 'outbox'], 'readwrite', async tx => {
      const settings = tx.objectStore('settings');
      if (await request(settings.get('backendURL')) === url) return;
      const outbox = tx.objectStore('outbox');
      await request(outbox.clear());
      await new Promise<void>((resolve, reject) => {
        const cursor = tx.objectStore('events').openCursor();
        cursor.onerror = () => reject(cursor.error);
        cursor.onsuccess = () => {
          if (!cursor.result) { resolve(); return; }
          const r = cursor.result.value as StoredRecord;
          const write = outbox.put({ key: r.key, source: r.source, bytes: r.bytes });
          write.onerror = () => reject(write.error);
          write.onsuccess = () => cursor.result!.continue();
        };
      });
      const sessions = tx.objectStore('sessions');
      for (const s of await request<Session[]>(sessions.getAll())) {
        await request(sessions.put({ ...s, ack: '0', pendingBytes: s.totalBytes }));
      }
      await request(settings.put(url, 'backendURL'));
    });
  }
  async create(tabId: number, raw: boolean, parentSource?: string): Promise<Session> {
    const session: Session = { id: crypto.randomUUID(), tabId, raw, createdAt: new Date().toISOString(),
      state: 'active', last: '0', ack: '0', totalBytes: 0, pendingBytes: 0,
      stream: parentSource ? 'business' : 'network', ...(parentSource ? { parentSource } : {}) };
    await transaction(this.db, ['sessions'], 'readwrite', async tx => {
      await request(tx.objectStore('sessions').add(session));
    });
    return session;
  }
  async sessions(): Promise<Session[]> {
    return transaction(this.db, ['sessions'], 'readonly', tx => request(tx.objectStore('sessions').getAll()));
  }
  async update(id: string, state: Session['state'], error?: string): Promise<void> {
    await transaction(this.db, ['sessions'], 'readwrite', async tx => {
      const store = tx.objectStore('sessions');
      const s = await request<Session | undefined>(store.get(id));
      if (!s) throw Error('Unknown capture source');
      await request(store.put({ ...s, state, error }));
    });
  }
  async append(source: string, type: Kind, data: NetworkData, payload?: StoredRecord['payload']): Promise<RecordEnvelope> {
    return transaction(this.db, ['sessions', 'events', 'outbox'], 'readwrite', async tx => {
      const sessions = tx.objectStore('sessions');
      const s = await request<Session | undefined>(sessions.get(source));
      if (!s) throw Error('Unknown capture source');
      const last = sequence(s.last);
      if (last === MAX_U64) throw Error('Sequence exhausted; start a new capture');
      const next = last + 1n;
      if (this.core.assess_sequence(last, next) !== 0) throw Error('WASM sequence check failed');
      const record: RecordEnvelope = { schema: SCHEMA, type, source_id: source, sequence: String(next),
        captured_at: new Date().toISOString(), data };
      const localPayload = s.raw ? payload : undefined;
      const bytes = new TextEncoder().encode(JSON.stringify({ record, payload: localPayload })).length;
      if (!this.core.fits_budget(BigInt(s.totalBytes), BigInt(bytes), BigInt(this.limit))) {
        throw Error('Local archive limit reached; export records before starting another capture');
      }
      const stored: StoredRecord = { key: recordKey(source, String(next)), source, record, bytes,
        ...(localPayload ? { payload: localPayload } : {}) };
      await request(tx.objectStore('events').add(stored));
      await request(tx.objectStore('outbox').add({ key: stored.key, source, bytes }));
      await request(sessions.put({ ...s, last: String(next), totalBytes: s.totalBytes + bytes,
        pendingBytes: s.pendingBytes + bytes }));
      return record;
    });
  }
  async appendBusiness(source: string, events: BusinessEventInput[], checkpoint?: Record<string, unknown>): Promise<void> {
    await transaction(this.db, ['sessions', 'events', 'outbox'], 'readwrite', async tx => {
      const store = tx.objectStore('sessions');
      const s = await request<Session | undefined>(store.get(source));
      if (!s || s.stream !== 'business') throw Error('Invalid business source');
      let last = sequence(s.last), total = s.totalBytes, pending = s.pendingBytes;
      for (const input of events) {
        const next = last + 1n;
        if (next > MAX_U64 || this.core.assess_sequence(last, next)) throw Error('Business sequence exhausted');
        const record = { schema: BUSINESS_SCHEMA, type: 'business_event' as const, adapter_version: 'wpk/1' as const,
          source_id: source, sequence: String(next), captured_at: new Date().toISOString(), ...input };
        wpkCall(this.core, { op: 'validate', record });
        const bytes = new TextEncoder().encode(JSON.stringify({ record })).length;
        if (!this.core.fits_budget(BigInt(total), BigInt(bytes), BigInt(this.limit))) throw Error('Business archive limit');
        const key = recordKey(source, String(next));
        await request(tx.objectStore('events').add({ key, source, record, bytes }));
        await request(tx.objectStore('outbox').add({ key, source, bytes }));
        last = next; total += bytes; pending += bytes;
      }
      await request(store.put({ ...s, last: String(last), totalBytes: total, pendingBytes: pending,
        ...(checkpoint ? { checkpoint } : {}) }));
    });
  }
  async batch(streams: ('network' | 'business')[] = ['network', 'business'], after?: string): Promise<{ source: Session; events: CaptureRecord[] } | null> {
    let sessions = (await this.sessions()).sort((a, b) => a.createdAt.localeCompare(b.createdAt));
    const index = sessions.findIndex(s => s.id === after);
    if (index >= 0) sessions = [...sessions.slice(index + 1), ...sessions.slice(0, index + 1)];
    for (const source of sessions) {
      if (!streams.includes(source.stream ?? 'network')) continue;
      const events = await transaction(this.db, ['outbox', 'events'], 'readonly', async tx => {
        const refs = await request<{ key: [string, string] }[]>(tx.objectStore('outbox').index('source').getAll(source.id, 64));
        const records = await Promise.all(refs.map(ref => request<StoredRecord>(tx.objectStore('events').get(ref.key))));
        const result: CaptureRecord[] = []; let bytes = 512;
        for (const record of records) {
          const size = new TextEncoder().encode(JSON.stringify(record.record)).length + 1;
          if (bytes + size > 240 * 1024) break;
          bytes += size; result.push(record.record);
        }
        if (records.length && !result.length) throw Error('One queued record exceeds the batch limit');
        return result;
      });
      if (events.length) return { source, events };
    }
    return null;
  }
  async acknowledge(source: string, through: string, sentThrough: string): Promise<void> {
    await transaction(this.db, ['sessions', 'outbox'], 'readwrite', async tx => {
      const store = tx.objectStore('sessions');
      const s = await request<Session | undefined>(store.get(source));
      if (!s) throw Error('ACK source is unknown');
      const ack = sequence(through), sent = sequence(sentThrough);
      if (sent > sequence(s.last) || this.core.validate_ack(sequence(s.ack), sent, ack) > 1) {
        throw Error('ACK regressed or exceeds the sent batch');
      }
      const range = IDBKeyRange.bound(recordKey(source, '0'), recordKey(source, through));
      const refs = await request<{ key: [string, string]; bytes: number }[]>(tx.objectStore('outbox').getAll(range));
      for (const ref of refs) await request(tx.objectStore('outbox').delete(ref.key));
      await request(store.put({ ...s, ack: through, pendingBytes: s.pendingBytes - refs.reduce((n, r) => n + r.bytes, 0) }));
    });
  }
  async acknowledgedBusiness(): Promise<{ source: Session; events: CaptureRecord[] } | null> {
    return transaction(this.db, ['sessions', 'events', 'outbox'], 'readonly', async tx => {
      const sessions = await request<Session[]>(tx.objectStore('sessions').getAll());
      const source = sessions.filter(s => s.stream === 'business' && s.state === 'active')
        .sort((a, b) => b.createdAt.localeCompare(a.createdAt))[0];
      if (!source || source.last === '0' || source.last !== source.ack || source.pendingBytes
        || await request(tx.objectStore('outbox').index('source').count(source.id))) return null;
      const stored = await request<StoredRecord | undefined>(tx.objectStore('events').get(recordKey(source.id, source.last)));
      if (!stored || stored.record.type !== 'business_event') throw Error('Acknowledged business event is missing');
      return { source, events: [stored.record] };
    });
  }
  async records(source: string): Promise<StoredRecord[]> {
    return transaction(this.db, ['events'], 'readonly', tx => request(tx.objectStore('events').index('source').getAll(source)));
  }
  async recent(source: string): Promise<CaptureRecord[]> {
    return transaction(this.db, ['events'], 'readonly', tx => new Promise((resolve, reject) => {
      const records: CaptureRecord[] = [];
      const cursor = tx.objectStore('events').index('source').openCursor(source, 'prev');
      cursor.onerror = () => reject(cursor.error);
      cursor.onsuccess = () => {
        if (!cursor.result || records.length === 10) { resolve(records); return; }
        records.push((cursor.result.value as StoredRecord).record); cursor.result.continue();
      };
    }));
  }
}
