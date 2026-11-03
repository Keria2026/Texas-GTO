import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
const root = new URL('../evidence/wpk/', import.meta.url);
const manifest = JSON.parse(await readFile(new URL('provenance.json', root), 'utf8'));
for (const [path, expected] of Object.entries(manifest.files_sha256)) {
  const bytes = await readFile(new URL(path, root));
  assert.equal(createHash('sha256').update(bytes).digest('hex'), expected, `Frozen source changed: ${path}`);
}
console.log(`Verified ${Object.keys(manifest.files_sha256).length} frozen WPK source/evidence hashes.`);
