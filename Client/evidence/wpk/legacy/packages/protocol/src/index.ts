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

const ReviewIntegerSchema = z.number().int().min(Number.MIN_SAFE_INTEGER).max(Number.MAX_SAFE_INTEGER);
const ReviewChipsSchema = ReviewIntegerSchema.nonnegative();
const ReviewIdSchema = z.string().min(1).max(128);
const ReviewStreetSchema = z.enum(["preflop", "flop", "turn", "river", "showdown"]);
const ReviewCardsSchema = z.array(CardSchema.nullable()).max(2);
const WpkReviewHandSchema = z.strictObject({
  handId: ReviewIdSchema, tableId: ReviewIdSchema, sourceHandNumber: ReviewIntegerSchema.positive(),
  status: z.literal("settled"), heroId: ReviewIdSchema,
  heroCards: z.tuple([CardSchema, CardSchema]), board: z.array(CardSchema).max(5),
  boards: z.array(z.strictObject({
    street: ReviewStreetSchema, cards: z.array(CardSchema).max(5),
    addedCards: z.array(CardSchema).min(1).max(5), time: z.iso.datetime(),
  })),
  players: z.array(z.strictObject({
    playerId: ReviewIdSchema, seat: ReviewIntegerSchema.min(1).max(8), position: ReviewIdSchema,
    revealedCards: ReviewCardsSchema, revealedAt: z.literal("settlement"),
    netChips: ReviewIntegerSchema, mushroomPaid: ReviewChipsSchema,
    mushroomAwarded: ReviewChipsSchema, cardType: z.string().nullable(),
  })).min(2).max(8),
  actions: z.array(z.strictObject({
    eventId: ReviewIdSchema, sourceEventId: z.string().regex(/^[0-9]{1,16}$/),
    seq: ReviewIntegerSchema.positive(), playerId: ReviewIdSchema,
    seat: ReviewIntegerSchema.min(1).max(8), street: ReviewStreetSchema,
    type: z.enum(["PAY_MUSHROOM", "SMALL_BLIND", "BIG_BLIND", "THIRD_BLIND", "ANTE", "BET", "RAISE", "CALL", "ALL_IN", "FOLD", "CHECK"]),
    amountDelta: ReviewChipsSchema.nullable(), amountReported: ReviewChipsSchema,
    amountTo: ReviewChipsSchema.nullable(), balanceAfter: ReviewChipsSchema.nullable(),
    source: z.enum(["notification", "history-backfill"]), time: z.iso.datetime(),
  })),
  totalPotReportedByHistory: ReviewChipsSchema,
  mushroom: z.strictObject({ paid: ReviewChipsSchema, awarded: ReviewChipsSchema, poolChange: ReviewIntegerSchema }),
  settledAt: z.iso.datetime(), evidence: z.array(ReviewIdSchema).min(1),
}).superRefine((hand, context) => {
  const issue = (message: string) => context.addIssue({ code: "custom", message });
  const players = new Map(hand.players.map((player) => [player.playerId, player]));
  if (players.size !== hand.players.length || !players.has(hand.heroId)
    || new Set(hand.players.map((p) => p.seat)).size !== hand.players.length) issue("Players and seats must be unique and include hero");
  if (hand.handId !== `${hand.tableId}:${hand.sourceHandNumber}`) issue("Hand identity disagrees with its source");
  if (![0, 3, 4, 5].includes(hand.board.length)) issue("Invalid final board length");
  const knownCards = [...hand.heroCards, ...hand.board, ...hand.players.flatMap((p) => p.revealedCards.filter((card) => card !== null))];
  if (new Set(knownCards).size !== knownCards.length) issue("Known cards must be distinct");
  if (hand.players.some((p) => ![0, 2].includes(p.revealedCards.length))
    || hand.players.find((p) => p.playerId === hand.heroId)?.revealedCards.length !== 0) issue("Hole-card visibility is inconsistent");
  let boardCount = 0;
  for (const board of hand.boards) {
    const count = { preflop: 0, flop: 3, turn: 4, river: 5, showdown: 5 }[board.street];
    if (board.cards.length !== count || board.cards.length !== boardCount + board.addedCards.length
      || board.cards.some((card, i) => card !== hand.board[i])
      || board.addedCards.some((card, i) => card !== hand.board[boardCount + i])) issue("Public-card history must match the final board");
    boardCount = board.cards.length;
  }
  if (boardCount !== hand.board.length) issue("Public-card history is incomplete");
  const eventIds = new Set<string>();
  let previousSourceId: bigint | null = null;
  let previousStreet = 0;
  const fees = new Map<string, bigint>();
  for (const [i, action] of hand.actions.entries()) {
    if (!/^[0-9]{1,16}$/.test(action.sourceEventId)) continue;
    const sourceId = BigInt(action.sourceEventId);
    if (action.seq !== i + 1 || (previousSourceId !== null && sourceId !== previousSourceId + 1n)) issue("Source actions must be contiguous and ordered");
    previousSourceId = sourceId;
    if (eventIds.has(action.eventId) || action.eventId !== `${hand.handId}:${action.sourceEventId}`) issue("Source event identity must be unique and stable");
    eventIds.add(action.eventId);
    const player = players.get(action.playerId);
    if (!player || player.seat !== action.seat) issue("Action actor disagrees with the player seats");
    const street = ReviewStreetSchema.options.indexOf(action.street);
    if (street < previousStreet) issue("Action streets cannot go backwards");
    previousStreet = street;
    if (action.type === "CHECK" || action.type === "FOLD") {
      if (action.amountReported !== 0 || action.amountDelta !== null || action.amountTo !== null) issue("Check and fold cannot debit chips");
    } else {
      if (action.amountDelta !== action.amountReported) issue("Debit amount must retain the reported delta");
      if (action.amountTo !== null && action.amountTo < action.amountReported) issue("Street total cannot be smaller than the debit increment");
    }
    if (action.type === "PAY_MUSHROOM") {
      if (action.amountTo !== null) issue("Mushroom fees are separate from street bets");
      fees.set(action.playerId, (fees.get(action.playerId) ?? 0n) + BigInt(action.amountReported));
    }
  }
  let paid = 0n, awarded = 0n, net = 0n;
  for (const player of hand.players) {
    if ((fees.get(player.playerId) ?? 0n) !== BigInt(player.mushroomPaid)) issue("Fee actions disagree with settlement");
    paid += BigInt(player.mushroomPaid); awarded += BigInt(player.mushroomAwarded); net += BigInt(player.netChips);
  }
  if (paid !== BigInt(hand.mushroom.paid) || awarded !== BigInt(hand.mushroom.awarded)
    || paid - awarded !== BigInt(hand.mushroom.poolChange) || net + paid - awarded !== 0n) issue("Player and mushroom ledgers must conserve chips");
});

export const WpkReviewSchema = z.strictObject({
  format: z.literal("wpk-offline-review-v1"), purpose: z.literal("completed-hand-review"),
  observedTable: z.strictObject({
    capacity: ReviewIntegerSchema.min(2).max(8), participantsInFirstCompletedHand: ReviewIntegerSchema.min(2).max(8),
    smallBlind: ReviewChipsSchema.positive(), bigBlind: ReviewChipsSchema.positive(),
    thirdBlindEnabled: z.boolean(), ante: ReviewChipsSchema,
  }).nullable(),
  completed: z.array(WpkReviewHandSchema),
  unfinished: z.array(z.strictObject({
    sourceHandNumber: ReviewIntegerSchema.positive(), status: z.literal("incomplete"),
    heroCards: ReviewCardsSchema, settlement: z.null(),
  })),
  rejectedFrames: z.array(z.strictObject({
    time: z.iso.datetime(), direction: z.enum(["sent", "received"]), reason: z.literal("decode-failed"),
  })),
}).superRefine((review, context) => {
  const issue = (message: string) => context.addIssue({ code: "custom", message });
  if (new Set(review.completed.map((hand) => hand.handId)).size !== review.completed.length) issue("Duplicate completed hand");
  if (new Set(review.completed.map((hand) => hand.tableId)).size > 1) issue("This review format supports one table");
  if (new Set(review.unfinished.map((hand) => hand.sourceHandNumber)).size !== review.unfinished.length
    || review.unfinished.some((hand) => review.completed.some((done) => done.sourceHandNumber === hand.sourceHandNumber))) issue("Completed and incomplete hands must be distinct");
  if (review.observedTable) {
    const capacity = review.observedTable.capacity;
    if (review.observedTable.participantsInFirstCompletedHand > capacity
      || review.completed.some((hand) => hand.players.some((p) => p.seat > capacity))) issue("Observed seats exceed table capacity");
    if (review.completed[0] && review.observedTable.participantsInFirstCompletedHand !== review.completed[0].players.length) issue("Observed participant count disagrees with the first hand");
  }
  if (review.unfinished.some((hand) => ![0, 2].includes(hand.heroCards.length)
    || new Set(hand.heroCards.filter((card) => card !== null)).size !== hand.heroCards.filter((card) => card !== null).length)) issue("Invalid incomplete-hand cards");
});

export type WpkReview = z.infer<typeof WpkReviewSchema>;

export const ObservedSnapshotSchema = z.strictObject({
  source_id: ReviewIdSchema, sequence: ReviewChipsSchema.positive(),
  table_id: ReviewIdSchema, hand_id: ReviewIdSchema, hero_id: ReviewIdSchema,
  street: z.enum(["preflop", "flop", "turn", "river", "showdown", "between_hands", "unknown"]),
  hero_cards: z.tuple([CardSchema.nullable(), CardSchema.nullable()]), board: z.array(CardSchema).max(5),
  players: z.array(z.strictObject({ player_id: ReviewIdSchema, seat: ReviewChipsSchema.min(1).max(8), stack: ReviewChipsSchema.nullable() })).min(1).max(8),
  actions: z.array(z.strictObject({
    source_event_id: z.string().regex(/^[0-9]{1,16}$/), player_id: ReviewIdSchema,
    action: z.enum(["PAY_MUSHROOM", "SMALL_BLIND", "BIG_BLIND", "THIRD_BLIND", "ANTE", "BET", "RAISE", "CALL", "ALL_IN", "FOLD", "CHECK"]),
    amount_delta: ReviewChipsSchema, amount_to: ReviewChipsSchema.nullable(), source_round: z.string().max(32).nullable(),
  })).max(256),
  to_act_player_id: ReviewIdSchema.nullable(),
  legal_actions: z.array(z.enum(["FOLD", "RAISE", "ALL_IN", "CALL", "CHECK", "BET"])).max(6),
  call_amount: ReviewChipsSchema.nullable(), min_raise_reported: ReviewChipsSchema.nullable(), max_raise_reported: ReviewChipsSchema.nullable(),
  history_complete: z.boolean(), rules_version: z.literal("wpk-observed-unverified-v1"),
}).superRefine((snapshot, context) => {
  const issue = (message: string) => context.addIssue({ code: "custom", message });
  const cards = [...snapshot.hero_cards.filter((c) => c !== null), ...snapshot.board];
  const players = new Set(snapshot.players.map((p) => p.player_id));
  if (new Set(cards).size !== cards.length || ![0, 3, 4, 5].includes(snapshot.board.length)) issue("Invalid visible cards");
  const counts: Record<string, number> = { preflop: 0, flop: 3, turn: 4, river: 5, between_hands: 0 };
  if (snapshot.street in counts && snapshot.board.length !== counts[snapshot.street]) issue("Observed street disagrees with the board");
  if (!["preflop", "flop", "turn", "river"].includes(snapshot.street) && snapshot.to_act_player_id !== null) issue("A non-betting phase cannot expose an actor");
  if (players.size !== snapshot.players.length || !players.has(snapshot.hero_id)
    || new Set(snapshot.players.map((p) => p.seat)).size !== snapshot.players.length) issue("Invalid observed players");
  if ((snapshot.to_act_player_id !== null && !players.has(snapshot.to_act_player_id))
    || (snapshot.legal_actions.length > 0 && snapshot.to_act_player_id !== snapshot.hero_id)
    || new Set(snapshot.legal_actions).size !== snapshot.legal_actions.length) issue("Invalid observed turn");
  let previous: bigint | null = null;
  for (const action of snapshot.actions) {
    if (!/^[0-9]{1,16}$/.test(action.source_event_id)) continue;
    const id = BigInt(action.source_event_id);
    if (!players.has(action.player_id) || (previous !== null && (id <= previous || (snapshot.history_complete && id !== previous + 1n)))) issue("Invalid observed action order");
    if (action.amount_to !== null && action.amount_to < action.amount_delta) issue("Invalid action amount");
    if (["FOLD", "CHECK"].includes(action.action) && action.amount_delta !== 0) issue("Check and fold cannot debit chips");
    previous = id;
  }
});

export const ResearchSocketEventSchema = z.discriminatedUnion("type", [
  z.strictObject({ type: z.literal("ready"), schema_version: z.literal("1.2") }),
  z.strictObject({ type: z.literal("response"), id: ReviewIdSchema, status: z.number().int().min(100).max(599), body: z.unknown() }),
  z.strictObject({ type: z.literal("error"), detail: z.string() }),
  z.strictObject({ type: z.literal("training"), body: TrainingJobSchema }),
  z.strictObject({ type: z.literal("observation"), active: z.boolean(), state_version: ReviewChipsSchema.positive(), snapshot: ObservedSnapshotSchema }),
  z.strictObject({ type: z.literal("observation_status"), active: z.literal(false) }),
  z.strictObject({ type: z.literal("observation_analysis"), state_version: ReviewChipsSchema.positive(),
    hand_strength: HandStrengthResponseSchema.nullable(),
    decision: z.strictObject({ status: z.literal("unsupported"), actions: z.tuple([]), missing_fields: z.array(z.string()) }),
    busy: z.boolean().optional(), error: z.string().optional(),
  }),
]);

export type ObservedSnapshot = z.infer<typeof ObservedSnapshotSchema>;
export type ResearchSocketEvent = z.infer<typeof ResearchSocketEventSchema>;
