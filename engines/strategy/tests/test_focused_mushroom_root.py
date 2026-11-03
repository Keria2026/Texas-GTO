"""Exact proposal checks and real eight-seat search with an explicit test policy."""

from collections import deque
from fractions import Fraction
from itertools import combinations
from math import comb, exp
from random import Random

import pytest
from test_mushroom_belief import SyntheticPolicy, observed_prefix, prefix_policy, strip_private

from texas_strategy.evaluation import CancellationError, TraversalBudget
from texas_strategy.mushroom_belief import MushroomFirstHandRoot, _sample_holes


def deals(cards, count):
    if count == 0:
        yield ()
        return
    for pair in combinations(cards, 2):
        remaining = tuple(card for card in cards if card not in pair)
        for tail in deals(remaining, count - 1):
            yield (tuple(sorted(pair)), *tail)


class ReplayRNG:
    def __init__(self, world, focus, branch):
        self.branch = branch
        self.remaining = deque(
            cards for seat, cards in enumerate(world) if branch == 1 or seat != focus
        )

    def randrange(self, stop):
        assert stop == 2
        return self.branch

    def sample(self, available, count):
        cards = self.remaining.popleft()
        assert count == 2 and all(card in available for card in cards)
        return cards


@pytest.mark.parametrize("focus_seat", [0, 1, 2])
def test_every_toy_deal_has_exact_mixture_density_and_unbiased_weight(focus_seat):
    deck = tuple(f"{rank}c" for rank in range(2, 10))
    focus_cards = ("2c", "3c")
    worlds = list(deals(deck, 3))
    prior = Fraction(1, len(worlds))
    probability_sum = exact_value = estimate = Fraction(0)
    for world in worlds:
        match = world[focus_seat] == focus_cards
        expected = prior / 2 + (prior * comb(8, 2) / 2 if match else 0)
        probability_sum += expected
        # A nonuniform posterior and a hidden-state payoff, independent of the sampler.
        likelihood = Fraction(int(world[0][0][0]) + 1, 10)
        value = int(world[1][1][0]) - int(world[2][0][0])
        exact_value += prior * likelihood * value
        estimate += expected * (prior * likelihood / expected) * value
        for branch in [0, 1] if match else [1]:
            rng = ReplayRNG(world, focus_seat, branch)
            sampled, p, q = _sample_holes(rng, deck, 3, (focus_seat, focus_cards))
            assert sampled == world and not rng.remaining
            assert p == pytest.approx(float(prior), rel=1e-14, abs=0)
            assert q == pytest.approx(float(expected), rel=1e-14, abs=0)
        assert expected > 0
    assert len(worlds) == 2520 and probability_sum == 1
    assert estimate == exact_value


def test_focus_is_not_a_fixed_private_hand_and_keeps_all_blockers():
    model, actual, history = observed_prefix(button=5, flop=True)
    public = strip_private(actual)
    builder = MushroomFirstHandRoot(
        model,
        strip_private(actual),
        prefix_policy(model, actual, history),
        budget=TraversalBudget(max_nodes=100_000),
    )
    target = actual.underlying.table.hand.state.hole_cards[public.to_act]
    local = public.to_act
    focus = local, tuple(sorted(target))
    seen, matches = set(), 0
    rng = Random(98)
    prior = 1.0
    for seat in range(8):
        prior /= comb(49 - 2 * seat, 2)
    for _ in range(120):
        state, q, w = builder._sample(rng, focus)
        assert state is not None and w > 0
        holes = state.underlying.table.hand.state.hole_cards
        cards = tuple(card for pair in holes for card in pair)
        assert len(cards) == len(set(cards)) == 16 and not set(cards) & set(public.board)
        assert strip_private(state) == public
        match = tuple(sorted(holes[local])) == focus[1]
        assert q == pytest.approx(prior * (1 + (comb(49, 2) if match else 0)) / 2, rel=1e-12, abs=0)
        # Conditioning changes the proposal only, never the target public posterior.
        likelihood = 1.0
        for physical, pair in zip(builder._participants, holes, strict=True):
            if physical in builder._likelihoods:
                likelihood *= exp(builder._likelihoods[physical][tuple(sorted(pair))])
        assert w == pytest.approx(prior * likelihood, rel=1e-12, abs=0)
        seen.add(holes[local])
        matches += match
    assert 35 < matches < 85 and len(seen) > 20


def test_prefix_reads_each_abstract_policy_entry_once_per_action():
    model, actual, history = observed_prefix(actions=1)
    policy = prefix_policy(model, actual, history)
    MushroomFirstHandRoot(model, strip_private(actual), policy, budget=TraversalBudget())
    assert policy.lookups == len(policy.entries) == 169


def test_focus_weights_do_not_clip_non_target_worlds_or_change_zero_mass():
    model, actual, history = observed_prefix()
    builder = MushroomFirstHandRoot(
        model,
        strip_private(actual),
        prefix_policy(model, actual, history),
        budget=TraversalBudget(max_nodes=10_000),
    )
    local = actual.underlying.table.hand.state.to_act
    target = tuple(sorted(actual.underlying.table.hand.state.hole_cards[local]))
    ratios = set()
    for seed in range(16):
        state, q, w = builder._sample(Random(seed), (local, target))
        match = tuple(sorted(state.underlying.table.hand.state.hole_cards[local])) == target
        expected = 2 / (1 + comb(52, 2)) if match else 2
        assert w / q == pytest.approx(expected)
        ratios.add(round(w / q, 8))
    assert len(ratios) == 2
    builder._likelihoods[builder._physical_actor] = {pair: float("-inf") for pair in builder._pairs}
    before = builder._budget.nodes
    state, q, w = builder._sample(Random(0), (local, target))
    assert state is None and q > 0 and w == 0
    assert builder._budget.nodes == before + 1


def test_targeted_eight_player_search_returns_exact_hero_key_with_other_hands_present():
    model, actual, _ = observed_prefix()

    class ExplicitResearchPolicy(SyntheticPolicy):
        """Defined uniform continuation for algorithm testing, never a production fallback."""

        def contains(self, game, state):
            return True

        def probabilities(self, game, state):
            actions = game.legal_actions(state)
            return (1 / len(actions),) * len(actions)

    budget = TraversalBudget(max_nodes=100_000, deadline_seconds=15)
    builder = MushroomFirstHandRoot(
        model, strip_private(actual), ExplicitResearchPolicy(model, {}), budget=budget
    )
    table = actual.underlying.table
    local = table.hand.state.to_act
    result = builder.solve(
        table.participants[local],
        table.hand.state.hole_cards[local],
        iterations=6,
        seed=17,
        max_nodes=100_000,
        max_rollout_nodes=100_000,
    )
    assert result.information_key == model.search_information_key(actual, model.actor(actual))
    assert result.actions == model.legal_actions(actual)
    key = result.player, result.subgame.root_information_key(result.information_key)
    assert result.policy.lookup(*key).probabilities == result.probabilities
    assert result.average_policy.lookup(*key) is not None
    assert sum(result.probabilities) == pytest.approx(1)
    assert result.diagnostics.completed_iterations == 6 and result.blueprint_fallback_queries == 0
    assert result.subgame.root_draws == 48
    root_keys = {key for player, key in result.policy.entries if player == result.player}
    assert len(root_keys) > 1
    budget.should_cancel = lambda: True
    with pytest.raises(CancellationError):
        result.root_distribution.sample(Random(0))
