"""Policy-induced public roots for the first hand of a mushroom model window.

All dealt players, including the observer and folded players, remain random.
The caller supplies a verified public prefix and keeps its policy snapshot open.
Later model hands require a joint model of previous private histories; they are
not reconstructed by replacing those histories with empty tuples here.
"""

from itertools import combinations
from math import comb, exp, isfinite, log

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
from texas_strategy.search import SampledRoot, SearchCoverageError, solve_generated_subgame


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
        self.public_key = mushroom_public_key(
            rules_version=model.base_game.rules_version,
            stacks=self._stacks(public_state),
            pool=opening.pool,
            outstanding=opening.ledger.snapshot().outstanding_contributions,
            button=opening.button,
            participants=opening.participants,
            waiting_for_top_up=opening.waiting_for_top_up,
            hand_number=1,
            ended=False,
            history=(),
            hand=self._public_game.public_key(public_state),
        )
        self._likelihoods = self._prepare_likelihoods()
        self.root = SampledRoot(self.public_key, self.actor, self._sample)
        self._check()

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
                if key not in probabilities:
                    entry = self._policy.lookup(player, key)
                    self._check()
                    if entry is None:
                        raise SearchCoverageError("Policy lacks a public-action likelihood")
                    if not isinstance(entry, PolicyEntry) or entry.actions != tuple(mapping):
                        raise ValueError("Public-action policy has inconsistent legal actions")
                    probabilities[key] = entry.probabilities[entry.actions.index(label)]
                probability = probabilities[key]
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
    ):
        """Use the same policy and total wall deadline as likelihood preparation."""
        target = self.target_information_key(hero_seat, hero_cards)
        focus = self._participants.index(hero_seat), tuple(sorted(hero_cards))
        root = SampledRoot(self.public_key, self.actor, lambda rng: self._sample(rng, focus))
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
            should_cancel=self._check,
        )
