import assert from "node:assert/strict";
import test from "node:test";
import {
  AnalysisResponseSchema, SessionResponseSchema, EquityResponseSchema,
  TrainingJobSchema, BlueprintsResponseSchema,
  ProfilesResponseSchema,
  HandStrengthResponseSchema, AdaptationResponseSchema,
} from "../src/index.ts";

const session = {
  session_id: "simulation-1", state_version: 1,
  game: { game_id: "kuhn", num_players: 2, stack: 20, small_blind: 1, big_blind: 2, button: 0, abstraction: "none" },
  rules_version: "kuhn-2p-v1", actor: 0, terminal: false,
  legal_actions: ["check", "bet"], observation: { private_card: 1, public_history: [] }, payoffs: null,
};
const analysis = {
  schema_version: "1.1", request_id: "request-1", session_id: "simulation-1", state_version: 1,
  status: "ready", strategy_source: "blueprint", rules_version: "kuhn-2p-v1", model_version: "model-1",
  actions: [{ action: "check", frequency: 0.75 }, { action: "bet", frequency: 0.25 }],
  warnings: [], diagnostics: { iterations: 1000 },
};

test("simulation snapshots retain opaque observations but validate actionable state", () => {
  assert.deepEqual(SessionResponseSchema.parse(session), session);
  assert.deepEqual(SessionResponseSchema.parse({ ...session, actor: null, terminal: true, legal_actions: [], payoffs: [1, -1] }).payoffs, [1, -1]);
  for (const invalid of [
    { ...session, state_version: -1 }, { ...session, actor: 2 },
    { ...session, terminal: true }, { ...session, actor: null },
    { ...session, actor: null, terminal: true }, { ...session, payoffs: [1] },
    { ...session, game: { ...session.game, num_players: 9 } },
    { ...session, extra: "unversioned" },
  ]) assert.throws(() => SessionResponseSchema.parse(invalid));
});

test("recommendations require normalized, unique actions and consistent provenance", () => {
  assert.deepEqual(AnalysisResponseSchema.parse(analysis), analysis);
  const unsupported = { ...analysis, status: "unsupported", strategy_source: "none", model_version: null, actions: [] };
  assert.deepEqual(AnalysisResponseSchema.parse(unsupported), unsupported);
  for (const invalid of [
    { ...analysis, actions: [] },
    { ...analysis, actions: [{ action: "bet", frequency: 0.2 }] },
    { ...analysis, actions: [{ action: "bet", frequency: 1.1 }] },
    { ...analysis, actions: [{ action: "bet", frequency: 0.5 }, { action: "bet", frequency: 0.5 }] },
    { ...analysis, actions: [{ action: "bet", frequency: Number.NaN }] },
    { ...analysis, status: "unsupported" },
    { ...analysis, strategy_source: "none" },
    { ...unsupported, strategy_source: "blueprint" },
    { ...analysis, schema_version: "1.0" },
  ]) assert.throws(() => AnalysisResponseSchema.parse(invalid));
});

test("training and equity bounds reject fabricated or malformed results", () => {
  const job = { job_id: "job-1", status: "completed", diagnostics: { nodes: 20 }, blueprint_id: "model-1", evaluation: null, error: null };
  assert.deepEqual(TrainingJobSchema.parse(job), job);
  assert.throws(() => TrainingJobSchema.parse({ ...job, status: "optimal" }));
  assert.deepEqual(BlueprintsResponseSchema.parse({ blueprints: [] }), { blueprints: [] });
  const equity = { equity: 0.5, stderr: 0.01, samples: 1000, method: "monte_carlo", seed: 0, opponents: 1, rejected_draws: 0, algorithm_version: "card-equity-v1" };
  assert.deepEqual(EquityResponseSchema.parse(equity), equity);
  for (const invalid of [{ ...equity, equity: 2 }, { ...equity, stderr: -1 }, { ...equity, samples: 0 }, { ...equity, equity: Infinity }]) {
    assert.throws(() => EquityResponseSchema.parse(invalid));
  }
});

test("profiles preserve simulation provenance and opportunity denominators", () => {
  const statistic = { successes: 1, opportunities: 1, posterior_mean: 2 / 3, interval: [0, 1], interval_method: "chebyshev_posterior_mass_bound", model_version: "observable-opponent-v1" };
  const profile = { schema_version: "1.1", session_id: "simulation-1", state_version: 3, model_version: "observable-opponent-v1", source: "simulation", hand_complete: true, players: [
    { player_id: "0", seat: 0, statistics: { VPIP: statistic }, actions: [] },
    { player_id: "1", seat: 1, statistics: {}, actions: [] },
  ], warnings: [] };
  assert.deepEqual(ProfilesResponseSchema.parse(profile), profile);
  assert.throws(() => ProfilesResponseSchema.parse({ ...profile, source: "live" }));
  assert.throws(() => ProfilesResponseSchema.parse({ ...profile, players: [{ ...profile.players[0], statistics: { VPIP: { ...statistic, successes: 3 } } }, profile.players[1]] }));
});

test("current hand analysis never invents a five-card rank before the flop", () => {
  const preflop = {
    hero: ["As", "Kd"], board: [], street: "preflop", current_rank: null,
    current_category: null, nuts: null, opponents: null, range_comparison: null,
    warnings: [], model_version: "current-hand-strength-v1", rules_version: "standard-high-holdem-v1",
    evaluation_scope: "current_street_only", equity_computed: false, action_ev_computed: false,
  };
  assert.deepEqual(HandStrengthResponseSchema.parse(preflop), preflop);
  for (const invalid of [
    { ...preflop, current_rank: [0, 14, 13, 12, 11, 9] },
    { ...preflop, current_category: "high_card" },
    { ...preflop, street: "flop" },
    { ...preflop, equity_computed: true },
    { ...preflop, hero: ["As", "As", "Kd"] },
    { ...preflop, hero: ["As", "As"] },
    { ...preflop, board: ["Qs", "Jh", "2c"] },
  ]) assert.throws(() => HandStrengthResponseSchema.parse(invalid));
});

test("adaptation outputs remain explicitly synthetic whole-game experiments", () => {
  const experiment = {
    schema_version: "1.1", blueprint_id: "model-1", hero: 0, opponent_mode: "call",
    model_source: "synthetic_bias", result: {
      accepted: false, selected_weight: 0, model_gain: 0, baseline_worst_case: null,
      candidate_worst_case: null, reason: "insufficient_samples", sample_count: 1,
    },
  };
  assert.deepEqual(AdaptationResponseSchema.parse(experiment), experiment);
  for (const invalid of [
    { ...experiment, model_source: "observed_players" },
    { ...experiment, hero: 3 },
    { ...experiment, result: { ...experiment.result, accepted: "yes" } },
    { ...experiment, result: { ...experiment.result, selected_weight: 1.2 } },
    { ...experiment, result: { ...experiment.result, model_gain: Infinity } },
  ]) assert.throws(() => AdaptationResponseSchema.parse(invalid));
});
