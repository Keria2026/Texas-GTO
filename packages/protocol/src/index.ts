import { z } from "zod";

const metadata = z.record(z.string(), z.unknown());
const action = z.string().min(1).max(128);
export const GameIdSchema = z.enum(["kuhn", "leduc", "holdem"]);
export const GameConfigSchema = z.strictObject({
  game_id: GameIdSchema,
  num_players: z.number().int().min(2).max(6),
  stack: z.number().int().positive(),
  small_blind: z.number().int().positive(),
  big_blind: z.number().int().positive(),
  button: z.number().int().min(0).max(5),
  abstraction: z.enum(["none", "suit_canonical"]),
});

export const HealthResponseSchema = z.strictObject({
  schema_version: z.literal("1.1"),
  status: z.literal("ok"),
  service: z.literal("texas-gto-analysis"),
  version: z.literal("0.1.0"),
  strategy: z.strictObject({
    provider: z.literal("pluribus_style"),
    status: z.literal("research_ready"),
    supported_games: z.tuple([z.literal("kuhn"), z.literal("leduc"), z.literal("holdem")]),
  }),
});

export const SessionResponseSchema = z.strictObject({
  session_id: z.string().min(1),
  state_version: z.number().int().nonnegative(),
  game: GameConfigSchema,
  rules_version: z.string().min(1),
  actor: z.number().int().min(0).max(5).nullable(),
  terminal: z.boolean(),
  legal_actions: z.array(action),
  observation: metadata,
  payoffs: z.array(z.number()).nullable(),
}).superRefine((value, context) => {
  if (value.terminal !== (value.actor === null)) {
    context.addIssue({ code: "custom", message: "Terminal status disagrees with the actor" });
  }
  if (value.actor !== null && value.actor >= value.game.num_players) {
    context.addIssue({ code: "custom", message: "Actor is outside the configured seats" });
  }
  if (value.terminal && value.legal_actions.length > 0) {
    context.addIssue({ code: "custom", message: "Terminal hands cannot expose actions" });
  }
  if (value.payoffs !== null && value.payoffs.length !== value.game.num_players) {
    context.addIssue({ code: "custom", message: "Payoffs must cover every player" });
  }
});

export const TrainingJobSchema = z.strictObject({
  job_id: z.string().min(1),
  status: z.enum(["queued", "running", "completed", "cancelled", "failed"]),
  diagnostics: metadata.nullable(),
  blueprint_id: z.string().nullable(),
  evaluation: metadata.nullable(),
  error: z.string().nullable(),
});

export const BlueprintsResponseSchema = z.strictObject({
  blueprints: z.array(z.strictObject({
    blueprint_id: z.string().min(1),
    metadata,
    information_sets: z.number().int().nonnegative(),
  })),
});

export const AnalysisResponseSchema = z.strictObject({
  schema_version: z.literal("1.1"),
  request_id: z.string().min(1),
  session_id: z.string().min(1),
  state_version: z.number().int().nonnegative(),
  status: z.enum(["ready", "unsupported"]),
  strategy_source: z.enum(["blueprint", "depth_limited_search", "none"]),
  rules_version: z.string().min(1),
  model_version: z.string().nullable(),
  actions: z.array(z.strictObject({ action, frequency: z.number().min(0).max(1) })),
  warnings: z.array(z.string()),
  diagnostics: metadata.nullable(),
}).superRefine((value, context) => {
  const sum = value.actions.reduce((total, item) => total + item.frequency, 0);
  if (value.status === "ready" && (value.actions.length === 0 || Math.abs(sum - 1) > 1e-6)) {
    context.addIssue({ code: "custom", message: "Ready strategies must sum to one" });
  }
  if (new Set(value.actions.map((item) => item.action)).size !== value.actions.length) {
    context.addIssue({ code: "custom", message: "Duplicate strategy action" });
  }
  if (value.status === "unsupported" && value.actions.length !== 0) {
    context.addIssue({ code: "custom", message: "Unsupported states cannot expose recommendations" });
  }
  if ((value.status === "ready") !== (value.strategy_source !== "none")) {
    context.addIssue({ code: "custom", message: "Strategy source disagrees with coverage" });
  }
});

export const EquityResponseSchema = z.strictObject({
  equity: z.number().min(0).max(1),
  stderr: z.number().nonnegative(),
  samples: z.number().int().positive(),
  method: z.string().min(1),
  seed: z.number().int(),
  opponents: z.number().int().min(1).max(5),
  rejected_draws: z.number().int().nonnegative(),
  algorithm_version: z.literal("card-equity-v1"),
});

const ProfileStatisticSchema = z.strictObject({
  successes: z.number().int().nonnegative(),
  opportunities: z.number().int().nonnegative(),
  posterior_mean: z.number().min(0).max(1),
  interval: z.tuple([z.number().min(0).max(1), z.number().min(0).max(1)]),
  interval_method: z.string().min(1),
  model_version: z.string().min(1),
}).superRefine((value, context) => {
  if (value.successes > value.opportunities || value.interval[0] > value.interval[1]) {
    context.addIssue({ code: "custom", message: "Invalid opportunity count or posterior interval" });
  }
});

export const ProfilesResponseSchema = z.strictObject({
  schema_version: z.literal("1.1"),
  session_id: z.string().min(1),
  state_version: z.number().int().nonnegative(),
  model_version: z.string().min(1),
  source: z.literal("simulation"),
  hand_complete: z.boolean(),
  players: z.array(z.strictObject({
    player_id: z.string().min(1),
    seat: z.number().int().min(0).max(5),
    statistics: z.record(z.string(), ProfileStatisticSchema),
    actions: z.array(z.strictObject({
      context: z.string().min(1),
      legal_actions: z.array(action),
      counts: z.record(z.string(), z.number().int().nonnegative()),
      probabilities: z.record(z.string(), z.number().min(0).max(1)),
      observations: z.number().int().nonnegative(),
      action_encoding: z.literal("family"),
      model_version: z.string().min(1),
    })),
  })).min(2).max(6),
  warnings: z.array(z.string()),
});

const CardSchema = z.string().regex(/^[2-9TJQKA][cdhs]$/);
const CategorySchema = z.enum([
  "high_card", "one_pair", "two_pair", "three_of_a_kind", "straight",
  "flush", "full_house", "four_of_a_kind", "straight_flush",
]);
const RankSchema = z.array(z.number().int().min(0).max(14)).min(2).max(6)
  .refine((rank) => rank[0] !== undefined && rank[0] <= 8);
const NutsBoundSchema = z.strictObject({
  best_rank: RankSchema, best_category: CategorySchema, hero_is_nuts: z.boolean(),
  combinations_evaluated: z.number().int().positive(),
});
const ComparisonSchema = z.strictObject({
  range_source: z.string(), combinations_evaluated: z.number().int().positive(),
  best_rank: RankSchema, best_category: CategorySchema,
  current_ahead_weight: z.number().min(0).max(1),
  current_tied_weight: z.number().min(0).max(1),
  current_behind_weight: z.number().min(0).max(1),
  ahead_combinations: z.number().int().nonnegative(),
  tied_combinations: z.number().int().nonnegative(),
  beating_combinations_count: z.number().int().nonnegative(),
  beating_combinations: z.array(z.strictObject({
    cards: z.tuple([CardSchema, CardSchema]), rank: RankSchema,
    category: CategorySchema, normalized_weight: z.number().min(0).max(1),
  })),
  beating_combinations_truncated: z.boolean(),
  normalization_underflow_combinations: z.number().int().nonnegative(),
  weight_normalization: z.literal("sum_to_one_after_known_card_blockers"),
}).superRefine((value, context) => {
  if (Math.abs(value.current_ahead_weight + value.current_tied_weight + value.current_behind_weight - 1) > 1e-6) {
    context.addIssue({ code: "custom", message: "Current comparison weights must sum to one" });
  }
});
export const HandStrengthResponseSchema = z.strictObject({
  hero: z.tuple([CardSchema, CardSchema]), board: z.array(CardSchema).max(5),
  street: z.enum(["preflop", "flop", "turn", "river"]),
  current_rank: RankSchema.nullable(), current_category: CategorySchema.nullable(),
  nuts: z.strictObject({ theoretical: NutsBoundSchema, blocker_aware: NutsBoundSchema, range: NutsBoundSchema.nullable() }).nullable(),
  opponents: ComparisonSchema.nullable(), range_comparison: ComparisonSchema.nullable(),
  warnings: z.array(z.string()), model_version: z.literal("current-hand-strength-v1"),
  rules_version: z.literal("standard-high-holdem-v1"),
  evaluation_scope: z.literal("current_street_only"), equity_computed: z.literal(false),
  action_ev_computed: z.literal(false),
}).superRefine((value, context) => {
  const preflop = value.street === "preflop";
  const requiredBoardCards = { preflop: 0, flop: 3, turn: 4, river: 5 }[value.street];
  if (value.board.length !== requiredBoardCards || new Set([...value.hero, ...value.board]).size !== value.hero.length + value.board.length) {
    context.addIssue({ code: "custom", message: "Street and distinct visible cards must agree" });
  }
  if (value.current_rank !== null && CategorySchema.options[value.current_rank[0] ?? -1] !== value.current_category) {
    context.addIssue({ code: "custom", message: "Current category disagrees with its rank" });
  }
  if (preflop !== (value.current_rank === null) || preflop !== (value.current_category === null) || preflop !== (value.nuts === null) || preflop !== (value.opponents === null)) {
    context.addIssue({ code: "custom", message: "Preflop hand strength must be undefined" });
  }
});

export const AdaptationResponseSchema = z.strictObject({
  schema_version: z.literal("1.1"), blueprint_id: z.string().min(1),
  hero: z.number().int().min(0).max(1), opponent_mode: z.enum(["call", "raise", "fold"]),
  model_source: z.literal("synthetic_bias"),
  result: z.looseObject({
    accepted: z.boolean(), selected_weight: z.number().min(0).max(1), model_gain: z.number(),
    baseline_worst_case: z.number().nullable(), candidate_worst_case: z.number().nullable(),
    reason: z.string(),
  }),
});

export type GameId = z.infer<typeof GameIdSchema>;
export type GameConfig = z.infer<typeof GameConfigSchema>;
export type HealthResponse = z.infer<typeof HealthResponseSchema>;
export type SessionResponse = z.infer<typeof SessionResponseSchema>;
export type TrainingJob = z.infer<typeof TrainingJobSchema>;
export type BlueprintsResponse = z.infer<typeof BlueprintsResponseSchema>;
export type AnalysisResponse = z.infer<typeof AnalysisResponseSchema>;
export type EquityResponse = z.infer<typeof EquityResponseSchema>;
export type ProfilesResponse = z.infer<typeof ProfilesResponseSchema>;
export type HandStrengthResponse = z.infer<typeof HandStrengthResponseSchema>;
export type AdaptationResponse = z.infer<typeof AdaptationResponseSchema>;

export function parseHealthResponse(input: unknown): HealthResponse {
  return HealthResponseSchema.parse(input);
}
