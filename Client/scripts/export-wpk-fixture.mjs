// Emits synthetic contract evidence; never reads a browser or player account.
import { fixture, normalize } from '../tests/wpk-fixture.ts';
const f = fixture(); f.fixture.play();
const { events, checkpoint } = normalize(f.messages);
process.stdout.write(JSON.stringify({
  description: 'Synthetic WPK client fixture from TGTO-FronEnd 2e72f34 with integration snapshot updates; production page observer + WASM reducer. No real player data.',
  events: events.map((e, i) => ({ schema: 'tgto.capture/0.1', type: 'business_event', adapter_version: 'wpk/1',
    source_id: 'synthetic-wpk', sequence: String(i + 1), captured_at: '2026-11-01T00:00:00Z', ...e })),
  expected: checkpoint,
}, null, 2) + '\n');
