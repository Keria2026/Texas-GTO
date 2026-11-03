"""Policy-induced public roots for the first hand of a mushroom model window.

All dealt players, including the observer and folded players, remain random.
The caller supplies a verified public prefix and keeps its policy snapshot open.
Later model hands require a joint model of previous private histories; they are
not reconstructed by replacing those histories with empty tuples here.
"""

from itertools import combinations
from math import comb, exp, isfinite, log
from time import monotonic

from texas_game_core.cards import DECK, validate_cards
from texas_game_core.mushroom_game import mushroom_information_key, mushroom_public_key
from texas_game_core.public_holdem import HoldemRules, PublicHoldemGame, PublicHoldemState
from texas_strategy.evaluation import EvaluationLimitError, TraversalBudget
from texas_strategy.mushroom_abstraction import (
    MushroomContextGame,
    context_action_map,
    encode_context,
)
from texas_strategy.mushroom_public import _history_labels
from texas_strategy.policy import PolicyEntry
from texas_strategy.search import (
    DepthLimitedGame,
    SampledRoot,
    SearchCoverageError,
    solve_generated_subgame,
)


def _sample_holes(rng, deck, players, focus=None):
    """Half uniform, half conditioned on one pair; return the *mixture* density."""
    available, holes = list(deck), [None] * players
    if focus is not None and rng.randrange(2) == 0:
        seat, cards = focus
        holes[seat] = cards
        for card in cards:
            available.remove(card)
    prior = 1.0
    for seat in range(players):
        prior /= comb(len(deck) - 2 * seat, 2)
        if holes[seat] is None:
            holes[seat] = tuple(sorted(rng.sample(available, 2)))
            for card in holes[seat]:
                available.remove(card)
    probability = prior
    if focus is not None:
        seat, cards = focus
        # A uniform joint deal gives any seat this exact pair with probability 1/C(N,2).
        probability *= (1 + (comb(len(deck), 2) if holes[seat] == cards else 0)) / 2
    return tuple(holes), prior, probability


class MushroomFirstHandRoot:
    """Prepare complete private-card likelihoods, then generate bounded public samples.

    A uniform deck prior is conditioned only on the observed public board.
    Known-board chance factors are common to every compatible private assignment
    and are absorbed into the unnormalized root scale. Each seat's accumulated
    log likelihood is shifted by its fixed maximum, never by a sampled maximum.
    """

    def __init__(self, model, public_state, policy, *, budget: TraversalBudget):
        if (
            not isinstance(model, MushroomContextGame)
            or type(public_state) is not PublicHoldemState
        ):
            raise ValueError("A mushroom context model and public hand state are required")
        if not isinstance(budget, TraversalBudget):
            raise ValueError("An explicit shared preparation/search budget is required")
        self._model, self._state, self._policy, self._budget = model, public_state, policy, budget
        self._check()
        metadata = policy.metadata
        if (
            metadata.get("game_id") != model.game_id
            or metadata.get("rules_version") != model.rules_version
            or metadata.get("num_players") != model.num_players
        ):
            raise ValueError("Public root policy does not match the model")
        opening = model.initial_state().underlying.table
        if opening.ended or opening.hand is None:
            raise SearchCoverageError("Model window has no playable first hand")
        game = opening.hand.game
        self._public_game = PublicHoldemGame(
            HoldemRules(
                game.initial_stacks,
                small_blind=game.small_blind,
                big_blind=game.big_blind,
                straddle=game.straddle,
                button=game.button,
            )
        )
        self._public_game.validate_state(public_state)
        _history_labels(
            self._public_game, public_state, opening.participants, opening.button, model.num_players
        )
        local_actor = self._public_game.actor(public_state)
        if local_actor in (None, -1):
            raise SearchCoverageError("Public root must be a player decision")
        self._participants = opening.participants
        self._physical_actor = self._participants[local_actor]
        self.actor = (self._physical_actor - model.base_game.button) % model.num_players
        self._outstanding = tuple(
            dict(opening.ledger.snapshot().outstanding_contributions).get(str(p), 0)
            for p in range(model.num_players)
        )
        self._opening = opening
        # shortcut: first model hand only; later hands need joint previous-private-history ranges.
        self._cards = tuple(card for card in DECK if card not in public_state.board)
        self._pairs = tuple(tuple(sorted(pair)) for pair in combinations(self._cards, 2))
        self.public_key = self.public_key_for(public_state)
        self._likelihoods = self._prepare_likelihoods()
        self.root = SampledRoot(self.public_key, self.actor, self._sample)
        self._check()

    def public_key_for(self, state):
        return mushroom_public_key(
            rules_version=self._model.base_game.rules_version,
            stacks=self._stacks(state),
            pool=self._opening.pool,
            outstanding=self._opening.ledger.snapshot().outstanding_contributions,
            button=self._opening.button,
            participants=self._opening.participants,
            waiting_for_top_up=self._opening.waiting_for_top_up,
            hand_number=1,
            ended=False,
            history=(),
            hand=self._public_game.public_key(state),
        )

    def historical_entry(self, player, physical, cards, state, key, actions):
        return self._policy.lookup(player, key)

    def likelihood_cache_key(self, key, cards, street):
        return key

    def _check(self):
        self._budget.check_time()
        return False

    def _stacks(self, state):
        stacks = list(self._opening.stacks)
        for local, seat in enumerate(self._participants):
            stacks[seat] = state.stacks[local]
        return tuple(stacks)

    def _board_until_decision(self, state):
        while self._public_game.actor(state) == -1:
            self._budget.tick()
            state = self._public_game.transition(
                state, f"deal:{self._state.board[len(state.board)]}"
            )
        return state

    def _prepare_likelihoods(self):
        logs = {}
        current = self._public_game.initial_state()
        labels = ()
        for event in self._state.history:
            current = self._board_until_decision(current)
            self._budget.tick()
            mapping = context_action_map(self._public_game, current)
            if event.action not in mapping.values():
                raise SearchCoverageError(
                    "Observed action is outside the policy action abstraction"
                )
            label = next(name for name, action in mapping.items() if action == event.action)
            physical = self._participants[event.player]
            player = (physical - self._model.base_game.button) % self._model.num_players
            weights = logs.setdefault(physical, dict.fromkeys(self._pairs, 0.0))
            probabilities = {}
            for cards in self._pairs:
                self._budget.tick()
                # A proven zero prefix stays unreachable; later likelihoods cannot revive it.
                if weights[cards] == float("-inf"):
                    continue
                key = encode_context(
                    num_players=self._model.num_players,
                    physical=physical,
                    button=self._opening.button,
                    betting_round=current.street,
                    cards=cards,
                    hand=current,
                    stacks=self._stacks(current),
                    outstanding=self._outstanding,
                    participants=self._participants,
                    waiting_for_top_up=self._opening.waiting_for_top_up,
                    pool=self._opening.pool,
                    actions=labels,
                    legal_actions=mapping,
                )
                cache_key = self.likelihood_cache_key(key, cards, current.street)
                if cache_key not in probabilities:
                    entry = self.historical_entry(
                        player, physical, cards, current, key, tuple(mapping)
                    )
                    self._check()
                    if entry is None:
                        raise SearchCoverageError("Policy lacks a public-action likelihood")
                    if not isinstance(entry, PolicyEntry) or entry.actions != tuple(mapping):
                        raise ValueError("Public-action policy has inconsistent legal actions")
                    probabilities[cache_key] = entry.probabilities[entry.actions.index(label)]
                probability = probabilities[cache_key]
                weights[cards] = (
                    float("-inf") if probability == 0 else weights[cards] + log(probability)
                )
            labels += (
                (
                    current.street,
                    (physical - self._opening.button) % self._model.num_players,
                    label,
                ),
            )
            current = self._public_game.transition(current, event.action)
        for weights in logs.values():
            self._check()
            maximum = max(weights.values())
            if maximum == float("-inf"):
                raise SearchCoverageError("Observed history has zero policy reach for a player")
            for cards, value in weights.items():
                weights[cards] = value - maximum
        return logs

    def _sample(self, rng, focus=None):
        self._budget.tick()
        holes, prior, q = _sample_holes(rng, self._cards, len(self._participants), focus)
        log_likelihood = 0.0
        for physical, cards in zip(self._participants, holes, strict=True):
            self._check()
            if physical in self._likelihoods:
                log_likelihood += self._likelihoods[physical][cards]
        if log_likelihood == float("-inf"):
            return None, q, 0.0
        weight = prior * exp(log_likelihood)
        if not isfinite(weight) or weight <= 0:
            raise EvaluationLimitError("Positive public-root weight cannot be represented")
        state = self._model.initial_state()
        while self._model.actor(state) == -1:
            self._budget.tick()
            hand = state.underlying.table.hand.state
            recipient = hand.pending_deal[0]
            if recipient == -1:
                raise ValueError("Unexpected public deal before the first betting decision")
            card = holes[recipient][len(hand.hole_cards[recipient])]
            state = self._model.transition(state, f"deal:{card}")
        for event in self._state.history:
            state = self._deal_observed_board(state)
            self._budget.tick()
            state = self._model.transition(state, event.action)
        state = self._deal_observed_board(state)
        if (
            self._model.public_key(state) != self.public_key
            or self._model.actor(state) != self.actor
        ):
            raise ValueError("Generated world does not reproduce the verified public prefix")
        self._check()
        return state, q, weight

    def _deal_observed_board(self, state):
        while self._model.actor(state) == -1:
            self._budget.tick()
            hand = state.underlying.table.hand.state
            if hand.pending_deal[0] != -1 or len(hand.board) >= len(self._state.board):
                raise ValueError("Generated world crossed the observed public prefix")
            state = self._model.transition(state, f"deal:{self._state.board[len(hand.board)]}")
        return state

    def target_information_key(self, hero_seat, hero_cards):
        if type(hero_seat) is not int or hero_seat != self._physical_actor:
            raise ValueError("The observer must be the current physical acting seat")
        if not isinstance(hero_cards, tuple) or len(hero_cards) != 2:
            raise ValueError("Exactly two known observer cards are required")
        if validate_cards(hero_cards + self._state.board) != hero_cards + self._state.board:
            raise ValueError("Observer cards must be canonical and distinct from the board")
        return mushroom_information_key(hero_seat, (), hero_cards, self.public_key)

    def solve(
        self,
        hero_seat,
        hero_cards,
        *,
        iterations=100,
        seed=0,
        max_nodes=1_000_000,
        max_rollout_nodes=250_000,
        search_seconds=None,
    ):
        """Use the same policy and total wall deadline as likelihood preparation."""
        target = self.target_information_key(hero_seat, hero_cards)
        focus = self._participants.index(hero_seat), tuple(sorted(hero_cards))
        root = SampledRoot(self.public_key, self.actor, lambda rng: self._sample(rng, focus))
        self._check()
        remaining = (
            None
            if self._budget.deadline_seconds is None
            else self._budget.deadline_seconds - (monotonic() - self._budget.started)
        )
        if remaining is not None and remaining <= 0:
            raise EvaluationLimitError("Public root preparation exhausted the search budget")
        return solve_generated_subgame(
            self._model,
            root,
            self._policy,
            target_player=self.actor,
            target_information_key=target,
            iterations=iterations,
            seed=seed,
            max_nodes=max_nodes,
            max_rollout_nodes=max_rollout_nodes,
            deadline_seconds=remaining
            if search_seconds is None
            else min(remaining or search_seconds, search_seconds),
            should_cancel=self._check,
        )


class MushroomStreetRoot(MushroomFirstHandRoot):
    """Re-solve from the first decision of the current street, over every private hand.

    Earlier hero decisions on the observed path stay fixed. Cached current policies
    are used when available; otherwise those decisions are bound to the blueprint.
    Previous streets use their last search average, with explicit blueprint coverage
    required for any unvisited information set. No unknown entry becomes uniform.
    """

    def __init__(self, model, public_state, policy, *, budget, previous=()):
        opening = model.initial_state().underlying.table
        engine = opening.hand.game
        public = PublicHoldemGame(
            HoldemRules(
                engine.initial_stacks,
                small_blind=engine.small_blind,
                big_blind=engine.big_blind,
                straddle=engine.straddle,
                button=engine.button,
            )
        )
        public.validate_state(public_state)
        current = public.initial_state()
        self.previous = tuple(previous)
        self._average_streets = {r["street"] for r in self.previous if r["average"].entries}
        self._history_public_keys = {}
        self._history_blueprint = {}
        self.target_state = public_state
        for event in (*public_state.history, None):
            while public.actor(current) == -1:
                budget.tick()
                current = public.transition(
                    current, f"deal:{public_state.board[len(current.board)]}"
                )
            if current.street == public_state.street:
                break
            if event is None:
                raise SearchCoverageError("Current street has no observed start")
            current = public.transition(current, event.action)
        self.root_history = len(current.history)
        self.target_depth = len(public_state.history) - self.root_history
        if self.target_depth > 64:
            raise EvaluationLimitError("Observed street exceeds the bounded recursive search depth")
        replayed = current
        for event in public_state.history[self.root_history :]:
            replayed = public.transition(replayed, event.action)
        if replayed != public_state:
            raise SearchCoverageError("Street history does not reproduce the target state")
        super().__init__(model, current, policy, budget=budget)

    def _previous_entry(self, player, info, street, history_size, field):
        for record in reversed(self.previous):
            if record["street"] == street and record["root_history"] <= history_size:
                key = DepthLimitedGame.root_information_key(
                    info, history_size - record["root_history"]
                )
                entry = record[field].lookup(player, key)
                if entry is not None:
                    return entry
        return None

    def likelihood_cache_key(self, key, cards, street):
        return (key, cards) if street in self._average_streets else key

    def historical_entry(self, player, physical, cards, state, key, actions):
        if state.street in self._average_streets:
            if state not in self._history_public_keys:
                self._history_public_keys[state] = self.public_key_for(state)
            info = mushroom_information_key(physical, (), cards, self._history_public_keys[state])
            entry = self._previous_entry(player, info, state.street, len(state.history), "average")
            if entry is not None:
                return entry
        # Exact-hand search averages stay separate; only the pinned blueprint may share a bucket.
        cache_key = player, state, key
        if cache_key not in self._history_blueprint:
            entry = self._policy.lookup(player, key)
            adapt = getattr(self._policy, "public_entry", None)
            self._history_blueprint[cache_key] = (
                adapt(entry, physical, state, actions) if entry is not None and adapt else entry
            )
        return self._history_blueprint[cache_key]

    def solve(
        self,
        hero_seat,
        hero_cards,
        *,
        iterations=100,
        seed=0,
        max_nodes=1_000_000,
        max_rollout_nodes=250_000,
        search_seconds=None,
    ):
        current_actor = self._public_game.actor(self.target_state)
        if current_actor in (None, -1) or self._participants[current_actor] != hero_seat:
            raise ValueError("The observer must act at the target decision")
        if not isinstance(hero_cards, tuple) or len(hero_cards) != 2:
            raise ValueError("Exactly two known observer cards are required")
        validate_cards(hero_cards + self.target_state.board)
        hero_pair = tuple(sorted(hero_cards))
        player = (hero_seat - self._model.base_game.button) % self._model.num_players
        target = mushroom_information_key(
            hero_seat, (), hero_cards, self.public_key_for(self.target_state)
        )

        def fixed(state, key, actions):
            if actions[0].startswith("continue:"):
                return None
            base = state.underlying
            table = base.underlying.table
            hand = table.hand.state
            if (
                key[0] == player
                and table.hand_number == self._opening.hand_number
                and tuple(sorted(hand.hole_cards[table.participants.index(hero_seat)])) == hero_pair
                and len(hand.history) < len(self.target_state.history)
                and (hand.history == self.target_state.history[: len(hand.history)])
            ):
                info = self._model.search_information_key(base, player)
                abstract = self._model.information_key(base, player)
                entry = self.published_entry(player, abstract)
                if entry is None:
                    entry = self._previous_entry(
                        player, info, hand.street, len(hand.history), "current"
                    )
                if entry is None:
                    entry = self._policy.lookup(player, abstract)
                if entry is None:
                    raise SearchCoverageError("Earlier hero strategy lacks coverage")
                return entry
            constrain = getattr(self._policy, "fixed_entry", None)
            return constrain(self._model, base) if constrain else None

        focus = self._participants.index(hero_seat), tuple(sorted(hero_cards))
        root = SampledRoot(self.public_key, self.actor, lambda rng: self._sample(rng, focus))
        self._check()
        remaining = (
            None
            if self._budget.deadline_seconds is None
            else (self._budget.deadline_seconds - (monotonic() - self._budget.started))
        )
        solved = solve_generated_subgame(
            self._model,
            root,
            self._policy,
            target_player=player,
            target_information_key=target,
            target_depth=self.target_depth,
            depth_limit=self.target_depth + 2,
            fixed_policy=fixed,
            iterations=iterations,
            seed=seed,
            max_nodes=max_nodes,
            max_rollout_nodes=max_rollout_nodes,
            deadline_seconds=remaining
            if search_seconds is None
            else min(remaining or search_seconds, search_seconds),
            should_cancel=self._check,
        )
        return solved

    def published_entry(self, player, abstract):
        for record in reversed(self.previous):
            if record.get("published") is not None:
                entry = record["published"].lookup(player, abstract)
                if entry is not None:
                    return entry
        return None

    def target_sampler(self, hero_seat, hero_cards):
        """Independent target posterior for full-horizon policy comparison."""
        focus = self._participants.index(hero_seat), tuple(sorted(hero_cards))

        def sample(rng):
            state, proposal, weight = self._sample(rng, focus)
            if state is None:
                return None, 0.0
            if tuple(sorted(state.underlying.table.hand.state.hole_cards[focus[0]])) != focus[1]:
                return None, 0.0
            for event in self.target_state.history[self.root_history :]:
                self._budget.tick()
                player = self._model.actor(state)
                physical = self._participants[event.player]
                info = self._model.search_information_key(state, player)
                entry = (
                    self.published_entry(player, self._model.information_key(state, player))
                    or self._previous_entry(
                        player,
                        info,
                        self.target_state.street,
                        len(state.underlying.table.hand.state.history),
                        "current",
                    )
                    if physical == hero_seat
                    else None
                )
                if entry is None:
                    entry = self._policy.lookup(player, self._model.information_key(state, player))
                    if entry is None:
                        raise SearchCoverageError("Evaluation history lacks policy coverage")
                    adapt = getattr(self._policy, "public_entry", None)
                    if adapt:
                        entry = adapt(
                            entry, physical, state.underlying.table.hand.state, entry.actions
                        )
                mapping = self._model.action_map(state)
                label = next((a for a, actual in mapping.items() if actual == event.action), None)
                if label is None or entry.actions != tuple(mapping):
                    raise SearchCoverageError("Evaluation action is outside the policy abstraction")
                probability = entry.probabilities[entry.actions.index(label)]
                product = weight * probability
                if product == 0 and weight > 0 and probability > 0:
                    raise EvaluationLimitError("Evaluation history weight underflow")
                weight = product
                state = self._model.transition(state, event.action)
            return (state, weight / proposal) if weight > 0 else (None, 0.0)

        return sample
