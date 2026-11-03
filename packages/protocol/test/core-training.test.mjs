import assert from "node:assert/strict";
import test from "node:test";
import { CoreTrainingResponseSchema } from "../src/index.ts";

test("automatic training status cannot claim a verified live strategy", () => {
  const status = {
    schema_version: "1.1", enabled: true, phase: "training", worker_pid: 123,
    game: {
      game_id: "mushroom", num_players: 8, stack: 800, stacks: null,
      small_blind: 2, big_blind: 4, straddle: 8, button: 0,
      abstraction: "none", session_hands: 2,
    },
    algorithm: "pluribus", objective: "expected_net_points", iterations: 1,
    information_sets: 100, averaged_information_sets: 0, checkpoint_bytes: 50000,
    quality: "finite_horizon_unvalidated", recommendation_ready: false, reason: null,
  };
  assert.deepEqual(CoreTrainingResponseSchema.parse(status), status);
  assert.throws(() => CoreTrainingResponseSchema.parse({ ...status, recommendation_ready: true }));
  assert.throws(() => CoreTrainingResponseSchema.parse({ ...status, objective: "win_rate" }));
});
