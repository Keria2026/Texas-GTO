"""Synthetic eight-player public prefixes, never substituted for live acceptance."""

from dataclasses import fields, replace
from itertools import combinations
from math import comb, prod
from random import Random

import pytest

from texas_game_core.cards import DECK
from texas_game_core.mushroom_game import MushroomSessionGame
from texas_game_core.public_holdem import HoldemRules, PublicHoldemGame, PublicHoldemState
from texas_strategy.blueprint import _trainer_metadata
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import CancellationError, EvaluationLimitError, TraversalBudget
from texas_strategy.mushroom_abstraction import MushroomContextGame
from texas_strategy.mushroom_belief import MushroomFirstHandRoot
from texas_strategy.policy import PolicyEntry, TabularPolicy, legal_information
from texas_strategy.search import DepthLimitedGame, SearchCoverageError


def deal_to_decision(model, state, rng):
    while model.actor(state) == -1:
        action, _ = model.sample_chance_outcome(state, rng)
        state = model.transition(state, action)
    return state


def observed_prefix(*, button=0, flop=False, actions=0, ante=0):
    model = MushroomContextGame(
        MushroomSessionGame((800,) * 8, button=button, horizon=2, ante=ante)
    )
    rng = Random(29)
    state = deal_to_decision(model, model.initial_state(), rng)
    history = []
    if flop:
        while model.betting_round(state) == 0:
            action = (
                "fold"
                if not history
                else ("check" if "check" in model.legal_actions(state) else "call")
            )
            history.append((state, action))
            state = deal_to_decision(model, model.transition(state, action), rng)
    for _ in range(actions):
        action = "check" if "check" in model.legal_actions(state) else "call"
        history.append((state, action))
        state = deal_to_decision(model, model.transition(state, action), rng)
    return model, state, history


def strip_private(state):
    hand = state.underlying.table.hand
    engine = hand.game
    public_game = PublicHoldemGame(
        HoldemRules(
            engine.initial_stacks,
            small_blind=engine.small_blind,
            big_blind=engine.big_blind,
            straddle=engine.straddle,
            ante=engine.ante,
            button=engine.button,
        )
    )
    values = {
        field.name: getattr(hand.state, field.name)
        for field in fields(PublicHoldemState)
        if field.name not in ("rules_version", "awaiting_settlement")
    }
    return PublicHoldemState(
        **values, rules_version=public_game.rules_version, awaiting_settlement=hand.state.terminal
    )


class SyntheticPolicy(TabularPolicy):
    """Explicit synthetic averages; missing keys stay strict, without uniform fallback."""

    def __init__(self, model, entries):
        super().__init__(entries, missing="error")
        self.metadata = _trainer_metadata(Trainer(model, algorithm="pluribus"))
        self.lookups = 0

    def lookup(self, player, information_key):
        self.lookups += 1
        return super().lookup(player, information_key)


def prefix_policy(model, state, history, *, zero=False):
    """Derive lookup keys from complete engine states, not the public encoder.

    Every compatible pair is substituted into the actual prefix actor's hand;
    all other seats receive distinct legal cards, including folded players.
    Exact engine validation checks those complete worlds before encoding them.
    """
    board = state.underlying.table.hand.state.board
    pairs = tuple(combinations(tuple(card for card in DECK if card not in board), 2))
    entries = {}
    for prefix, observed_action in history:
        table = prefix.underlying.table.fork()
        local_actor = table.hand.state.to_act
        player = model.actor(prefix)
        for pair in pairs:
            available = iter(card for card in DECK if card not in (*board, *pair))
            holes = tuple(
                tuple(pair) if seat == local_actor else (next(available), next(available))
                for seat in range(len(table.participants))
            )
            table.hand = replace(table.hand, state=replace(table.hand.state, hole_cards=holes))
            table.hand.game.validate_state(table.hand.state)
            world = replace(prefix, underlying=replace(prefix.underlying, table=table))
            key = player, model.information_key(world, player)
            actions = model.legal_actions(world)
            weights = [
                1 + (index + int(key[1].rsplit(":", 1)[-1][:2], 16)) % len(actions)
                for index in range(len(actions))
            ]
            if zero:
                weights[actions.index(observed_action)] = 0
            total = sum(weights)
            entry = PolicyEntry(actions, tuple(weight / total for weight in weights))
            assert key not in entries or entries[key] == entry
            entries[key] = entry
    return SyntheticPolicy(model, entries)


@pytest.mark.parametrize("button", [0, 5])
def test_initial_root_has_q_equal_w_and_is_independent_of_observer_cards(button):
    model, actual, history = observed_prefix(button=button)
    public = strip_private(actual)
    assert not hasattr(public, "hole_cards")
    policy = prefix_policy(model, actual, history)
    builder = MushroomFirstHandRoot(model, public, policy, budget=TraversalBudget())
    expected_q = prod(1 / comb(52 - 2 * seat, 2) for seat in range(8))
    first, q, w = builder.root.sample(Random(31))
    assert q == pytest.approx(expected_q, rel=1e-12, abs=0)
    assert q == w and policy.lookups == 0
    assert model.public_key(first) == model.public_key(actual) == builder.root.public_key
    assert model.actor(first) == builder.actor == model.actor(actual)
    table = actual.underlying.table
    hero = table.participants[table.hand.state.to_act]
    cards = table.hand.state.hole_cards[table.hand.state.to_act]
    assert builder.target_information_key(hero, cards) == model.search_information_key(
        actual, model.actor(actual)
    )
    again, q2, w2 = builder.root.sample(Random(31))
    assert (
        again.underlying.table.hand.state.hole_cards == first.underlying.table.hand.state.hole_cards
    )
    assert (q2, w2) == (q, w)
    seen = {
        builder.root.sample(Random(seed))[0].underlying.table.hand.state.hole_cards[
            table.hand.state.to_act
        ]
        for seed in range(12)
    }
    assert len(seen) > 1 and any(pair != cards for pair in seen)


@pytest.mark.parametrize(
    "stacks,button,waiting",
    [
        ((800,) * 4, 2, ()),
        ((800,) * 7 + (20,), 3, (7,)),
        ((800,) * 6 + (10, 20), 7, (6, 7)),
    ],
)
def test_partial_rosters_and_absent_button_keep_physical_seats_and_waiting_balances(
    stacks, button, waiting
):
    model = MushroomContextGame(MushroomSessionGame(stacks, button=button, horizon=2))
    first = deal_to_decision(model, model.initial_state(), Random(29))
    actual = deal_to_decision(model, model.transition(first, "call"), Random(31))
    public = strip_private(actual)
    policy = prefix_policy(model, actual, [(first, "call")])
    builder = MushroomFirstHandRoot(model, public, policy, budget=TraversalBudget())
    expected = actual.underlying.table
    participants = tuple(seat for seat in range(len(stacks)) if seat not in waiting)
    assert expected.participants == participants and expected.waiting_for_top_up == waiting
    hero = participants[public.to_act]
    actor = (hero - button) % len(stacks)
    assert builder.actor == model.actor(actual) == actor
    assert builder.target_information_key(
        hero, expected.hand.state.hole_cards[public.to_act]
    ) == model.search_information_key(actual, actor)
    for seed in range(8):
        sampled, q, w = builder.root.sample(Random(seed))
        table, inner = sampled.underlying.table, sampled.underlying.table.hand.state
        cards = tuple(card for pair in inner.hole_cards for card in pair)
        assert len(inner.hole_cards) == len(participants)
        assert len(cards) == len(set(cards)) == 2 * len(participants)
        assert table.participants == participants and table.waiting_for_top_up == waiting
        assert all(table.stacks[seat] == stacks[seat] for seat in waiting)
        assert table.ledger.snapshot() == expected.ledger.snapshot()
        assert strip_private(sampled) == public
        assert model.public_key(sampled) == builder.public_key == model.public_key(actual)
        assert model.actor(sampled) == actor
        assert builder.target_information_key(
            hero, inner.hole_cards[inner.to_act]
        ) == model.search_information_key(sampled, actor)
        assert 0 < w <= q


def test_flop_prefix_uses_only_then_visible_information_and_keeps_folded_blockers():
    model, actual, history = observed_prefix(flop=True, actions=1)
    public = strip_private(actual)
    assert len(public.board) == 3 and any(public.folded)
    policy = prefix_policy(model, actual, history)
    builder = MushroomFirstHandRoot(model, public, policy, budget=TraversalBudget())
    for seed in range(8):
        sampled, q, w = builder.root.sample(Random(seed))
        inner = sampled.underlying.table.hand.state
        cards = tuple(card for pair in inner.hole_cards for card in pair)
        assert len(cards) == len(set(cards)) == 16
        assert not set(cards) & set(public.board)
        assert all(len(inner.hole_cards[p]) == 2 for p, folded in enumerate(inner.folded) if folded)
        assert inner.folded == public.folded
        assert strip_private(sampled) == public
        assert model.public_key(sampled) == builder.public_key == model.public_key(actual)
        assert 0 < w <= q
    # Future board in a preflop likelihood would request a key absent from this strict fixture.
    assert policy.lookups > 0 and policy.fallback_count == 0


def test_nonuniform_observed_action_likelihood_changes_root_weight_by_one_fixed_scale():
    model, actual, history = observed_prefix(actions=1)
    policy = prefix_policy(model, actual, history)
    builder = MushroomFirstHandRoot(model, strip_private(actual), policy, budget=TraversalBudget())
    action = history[0][1]
    maximum = max(
        entry.probabilities[entry.actions.index(action)] for entry in policy.entries.values()
    )
    ratios = set()
    for seed in range(12):
        sampled, q, w = builder.root.sample(Random(seed))
        # Restore the sampled world's exact first decision using its actual private deal.
        initial = model.initial_state()
        holes = sampled.underlying.table.hand.state.hole_cards
        while model.actor(initial) == -1:
            hand = initial.underlying.table.hand.state
            player = hand.pending_deal[0]
            initial = model.transition(
                initial, f"deal:{holes[player][len(hand.hole_cards[player])]}"
            )
        key, _ = legal_information(model, initial)
        entry = policy.entries[key]
        expected = entry.probabilities[entry.actions.index(action)] / maximum
        assert w / q == pytest.approx(expected)
        ratios.add(round(w / q, 10))
    assert len(ratios) > 1


def test_missing_average_and_zero_probability_are_distinct_failures():
    model, actual, history = observed_prefix(actions=1)
    with pytest.raises(SearchCoverageError, match="lacks a public-action likelihood"):
        MushroomFirstHandRoot(
            model, strip_private(actual), SyntheticPolicy(model, {}), budget=TraversalBudget()
        )
    zero = prefix_policy(model, actual, history, zero=True)
    with pytest.raises(SearchCoverageError, match="zero policy reach"):
        MushroomFirstHandRoot(model, strip_private(actual), zero, budget=TraversalBudget())


def test_proven_zero_private_ranges_are_consumed_once_without_retrying():
    model, actual, history = observed_prefix(actions=1)
    covered = prefix_policy(model, actual, history)
    entries = {}
    for key, entry in covered.entries.items():
        if int(key[1][:2], 16) % 2:
            weights = list(entry.probabilities)
            weights[entry.actions.index(history[0][1])] = 0
            total = sum(weights)
            entry = PolicyEntry(entry.actions, tuple(weight / total for weight in weights))
        entries[key] = entry
    policy = SyntheticPolicy(model, entries)
    budget = TraversalBudget()
    builder = MushroomFirstHandRoot(model, strip_private(actual), policy, budget=budget)
    zeros = positives = 0
    for seed in range(30):
        before = budget.nodes
        state, q, w = builder.root.sample(Random(seed))
        assert q > 0
        if w == 0:
            assert state is None and budget.nodes == before + 1
            zeros += 1
        else:
            assert model.public_key(state) == builder.public_key
            positives += 1
    assert zeros and positives


def test_incompatible_action_order_cannot_be_used_as_a_likelihood():
    model, actual, history = observed_prefix(actions=1)
    covered = prefix_policy(model, actual, history)
    reversed_entries = {
        key: PolicyEntry(tuple(reversed(entry.actions)), tuple(reversed(entry.probabilities)))
        for key, entry in covered.entries.items()
    }
    with pytest.raises(ValueError, match="inconsistent legal actions"):
        MushroomFirstHandRoot(
            model,
            strip_private(actual),
            SyntheticPolicy(model, reversed_entries),
            budget=TraversalBudget(),
        )


def test_off_tree_public_bet_is_unsupported_instead_of_zero_reach():
    model, actual, _ = observed_prefix()
    actual = model.transition(actual, "raise_to:17")
    with pytest.raises(SearchCoverageError, match="outside the policy action abstraction"):
        MushroomFirstHandRoot(
            model, strip_private(actual), SyntheticPolicy(model, {}), budget=TraversalBudget()
        )


def test_preparation_and_sampling_share_strict_node_and_cancellation_budgets():
    model, actual, history = observed_prefix(actions=1)
    policy = prefix_policy(model, actual, history)
    with pytest.raises(EvaluationLimitError, match="node budget"):
        MushroomFirstHandRoot(
            model, strip_private(actual), policy, budget=TraversalBudget(max_nodes=1)
        )
    with pytest.raises(CancellationError):
        MushroomFirstHandRoot(
            model, strip_private(actual), policy, budget=TraversalBudget(should_cancel=lambda: True)
        )
    model, actual, history = observed_prefix()
    budget = TraversalBudget(max_nodes=1)
    builder = MushroomFirstHandRoot(
        model, strip_private(actual), prefix_policy(model, actual, history), budget=budget
    )
    with pytest.raises(EvaluationLimitError, match="node budget"):
        builder.root.sample(Random(0))


def test_wrong_policy_binding_and_observer_information_are_rejected():
    model, actual, history = observed_prefix()
    policy = prefix_policy(model, actual, history)
    public = strip_private(actual)
    builder = MushroomFirstHandRoot(model, public, policy, budget=TraversalBudget())
    hero = actual.underlying.table.participants[public.to_act]
    for seat, cards in ((True, ("As", "Kh")), ((hero + 1) % 8, ("As", "Kh")), (hero, ("As", "As"))):
        with pytest.raises(ValueError):
            builder.target_information_key(seat, cards)
    policy.metadata["rules_version"] = "wrong-rules"
    with pytest.raises(ValueError, match="does not match"):
        MushroomFirstHandRoot(model, public, policy, budget=TraversalBudget())


def test_eight_player_generated_roots_reach_real_two_hand_sampled_mccfr(monkeypatch):
    model, actual, _ = observed_prefix()
    completed_hand_numbers = set()
    terminal_payoffs = model.terminal_payoffs

    def observed_terminal_payoffs(state):
        completed_hand_numbers.add(state.underlying.table.hand_number)
        return terminal_payoffs(state)

    monkeypatch.setattr(model, "terminal_payoffs", observed_terminal_payoffs)

    class ExplicitUniformPolicy(SyntheticPolicy):
        """A deliberately defined research policy, not an untrained-table fallback."""

        def contains(self, game, state):
            return True

        def probabilities(self, game, state):
            actions = game.legal_actions(state)
            return (1 / len(actions),) * len(actions)

    policy = ExplicitUniformPolicy(model, {})
    builder = MushroomFirstHandRoot(model, strip_private(actual), policy, budget=TraversalBudget())

    def no_enumeration(*args):
        pytest.fail("Eight-player search must not enumerate hidden worlds or future deals")

    monkeypatch.setattr(model, "chance_outcomes", no_enumeration)
    subgame = DepthLimitedGame(model, builder.root, policy, depth_limit=1, sampled_rollouts=True)
    trainer = Trainer(subgame, algorithm="mccfr", seed=17)
    result = trainer.train(3, max_nodes=100_000, deadline_seconds=20)
    assert result.completed_iterations == 3
    assert subgame.root_draws == subgame.root_positive_draws == 24
    assert subgame.rollout_samples > 0 and subgame.rollout_cache_entries == 0
    assert trainer.average_policy(missing="error").entries
    assert policy.fallback_count == 0
    assert 2 in completed_hand_numbers
