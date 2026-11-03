import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const binary = await readFile(new URL('../target/wasm32-unknown-unknown/release/tgto_capture_core.wasm', import.meta.url));
const { instance } = await WebAssembly.instantiate(binary, {});
const core = instance.exports;

test('the actual packaged ABI loads without browser or network imports', () => {
  assert.equal(core.abi_version(), 3);
});

test('frame, ACK and storage checks run inside WASM', () => {
  assert.equal(core.validate_frame(2, 1024, 1024), 0);
  assert.equal(core.validate_frame(2, 1025, 1024), 2);
  assert.equal(core.validate_frame(3, 0, 1024), 1);
  assert.equal(core.validate_frame(9, 125, 1024), 0);
  assert.equal(core.validate_frame(9, 126, 1024), 4);
  assert.equal(core.validate_ack(4n, 8n, 9n), 3);
  assert.equal(core.validate_ack(4n, 8n, 3n), 2);
  assert.equal(core.validate_ack(4n, 8n, 4n), 1);
  assert.equal(core.fits_budget(4n, 6n, 10n), 1);
  assert.equal(core.fits_budget(18446744073709551615n, 1n, 18446744073709551615n), 0);
});

test('card validation catches duplicates, overlap and invalid sizes', () => {
  assert.equal(core.validate_cards(0n, 0n, 0, 0), 0);
  assert.equal(core.validate_cards(3n, 28n, 2, 3), 0);
  assert.equal(core.validate_cards(3n, 124n, 2, 5), 0);
  assert.ok(core.validate_cards(1n, 0n, 2, 0) & 8);
  assert.ok(core.validate_cards(3n, 7n, 2, 3) & 16);
  assert.ok(core.validate_cards(0n, 3n, 0, 2) & 4);
  assert.ok(core.validate_cards(1n << 52n, 0n, 0, 0) & 1);
});

test('u64 sequencing crosses the JS safe integer boundary without truncation', () => {
  assert.equal(core.assess_sequence(0n, 1n), 0);
  assert.equal(core.assess_sequence(7n, 7n), 1);
  assert.equal(core.assess_sequence(7n, 9n), 2);
  assert.equal(core.assess_sequence(9007199254740992n, 9007199254740993n), 0);
  assert.equal(core.assess_sequence(18446744073709551615n, 0n), 1);
});
