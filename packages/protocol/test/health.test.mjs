import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { parseHealthResponse } from "../src/index.ts";

const fixture = JSON.parse(
  await readFile(new URL("../../../fixtures/protocol/health-response.json", import.meta.url), "utf8"),
);

test("the shared fixture is accepted without claiming a trained solver", () => {
  assert.deepEqual(parseHealthResponse(fixture), fixture);
});

test("unknown versions, fabricated capability and extra fields are rejected", () => {
  for (const input of [
    { ...fixture, schema_version: "2.0" },
    { ...fixture, strategy: { ...fixture.strategy, status: "ready" } },
    { ...fixture, strategy: { ...fixture.strategy, supported_games: ["holdem"] } },
    { ...fixture, actions: [{ action: "raise", frequency: 1 }] },
    null,
  ]) {
    assert.throws(() => parseHealthResponse(input));
  }
});
