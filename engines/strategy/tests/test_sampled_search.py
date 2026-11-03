"""The sampled tail is the same joint continuation game, not a fixed noisy value."""

from random import Random
from statistics import mean

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import EvaluationLimitError, evaluate_policy, evaluate_strategy
from texas_strategy.policy import TabularPolicy
from texas_strategy.search import (
    DepthLimitedGame,
    RootDistribution,
    SearchCoverageError,
    build_public_root,
    solve_subgame,
)


class UniformPolicy:
    def probabilities(self, game, state):
        actions = game.legal_actions(state)
        return (1 / len(actions),) * len(actions)


def kuhn_tail(*, sampled=True, first="blueprint", second="blueprint", **kwargs):
    game = KuhnPoker()
    state = game.replay((2, 0))
    root = build_public_root(game, game.public_key(state), UniformPolicy())
    subgame = DepthLimitedGame(
        game, root, UniformPolicy(), depth_limit=1, sampled_rollouts=sampled, **kwargs
    )
    cutoff = subgame.transition(subgame.wrap_state(state), "check")
    chosen = subgame.transition(
        subgame.transition(cutoff, f"continue:{first}"), f"continue:{second}"
    )
    return subgame, chosen


def finish(subgame, state, rng):
    while subgame.actor(state) is not None:
        assert subgame.actor(state) == -1
        action, probability = subgame.sample_chance_outcome(state, rng)
        assert 0 < probability <= 1
        state = subgame.transition(state, action)
    return state, subgame.terminal_payoffs(state)


@pytest.mark.parametrize(
    "first,second,expected",
    [("blueprint", "blueprint", 0.75), ("call", "blueprint", 1.2), ("blueprint", "raise", 0.6)],
)
def test_chance_tail_exact_expectation_matches_independent_joint_calculation(
    first, second, expected
):
    sampled, start = kuhn_tail(first=first, second=second)
    exact, terminal = kuhn_tail(sampled=False, first=first, second=second)
    assert sampled.actor(start) == -1 and start.rollout
    # Enumerate the tiny chance-only tail to check its distribution without Monte Carlo error.
    assert evaluate_policy(sampled, UniformPolicy(), start_state=start) == pytest.approx(
        (expected, -expected)
    )
    assert exact.terminal_payoffs(terminal) == pytest.approx((expected, -expected))
    assert sampled.rollout_cache_entries == 0
    assert sampled.rules_version != exact.rules_version


def test_repeated_visits_draw_fresh_trajectories_with_returned_probabilities():
    subgame, start = kuhn_tail()
    rng = Random(4)
    values = [finish(subgame, start, rng)[1][0] for _ in range(4000)]
    assert set(values) == {-1.0, 1.0, 2.0}
    assert mean(values) == pytest.approx(0.75, abs=0.05)
    assert subgame.rollout_samples == 4000
    assert subgame.rollout_cache_entries == 0
    with pytest.raises(ValueError, match="no player information"):
        subgame.information_key(start, 1)


def test_sampled_continuation_choices_remain_private_and_include_every_active_player():
    game = KuhnPoker(3)
    state = game.replay((3, 1, 0))
    root = build_public_root(game, game.public_key(state), UniformPolicy())
    subgame = DepthLimitedGame(game, root, UniformPolicy(), depth_limit=1, sampled_rollouts=True)
    cutoff = subgame.transition(subgame.wrap_state(state), "bet")
    first = subgame.transition(cutoff, "continue:fold")
    second = subgame.transition(cutoff, "continue:raise")
    assert subgame.actor(first) == subgame.actor(second) == 1
    assert subgame.information_key(first, 1) == subgame.information_key(second, 1)
    assert subgame.observation(first, 1) == subgame.observation(second, 1)
    for mode in ("blueprint", "call"):
        first = subgame.transition(first, f"continue:{mode}")
    assert first.rollout and subgame.actor(first) == -1
    _, payoffs = finish(subgame, first, Random(9))
    assert len(payoffs) == 3 and sum(payoffs) == 0


def test_missing_and_zero_probability_continuations_are_never_invented():
    subgame, start = kuhn_tail()
    subgame.blueprint = TabularPolicy()
    with pytest.raises(SearchCoverageError, match="does not cover"):
        finish(subgame, start, Random(0))

    class CheckOnly:
        def probabilities(self, game, state):
            return (1.0, 0.0)

    subgame.blueprint = CheckOnly()
    assert subgame.chance_outcomes(start) == (("check", 1.0),)
    assert subgame.sample_chance_outcome(start, Random(0)) == ("check", 1.0)
    with pytest.raises(ValueError, match="zero continuation probability"):
        subgame.transition(start, "bet")


def test_node_limits_and_cancelled_iteration_preserve_policy_and_rng():
    subgame, start = kuhn_tail(max_rollout_nodes=1)
    after_bet = subgame.transition(start, "bet")
    with pytest.raises(EvaluationLimitError, match="node budget"):
        subgame.transition(after_bet, "call")

    subgame, _ = kuhn_tail(max_rollout_nodes=100_000)
    interrupted = Trainer(subgame, algorithm="mccfr", weighting="linear", seed=6)
    assert interrupted.train(1, max_nodes=20).completed_iterations == 0
    assert not interrupted._table
    interrupted.train(4)
    control_game, _ = kuhn_tail(max_rollout_nodes=100_000)
    direct = Trainer(control_game, algorithm="mccfr", weighting="linear", seed=6)
    direct.train(4)
    assert interrupted._rng.getstate() == direct._rng.getstate()
    assert interrupted._table == direct._table


def mushroom_root():
    class SampleOnly(MushroomSessionGame):
        def chance_outcomes(self, state):
            raise AssertionError("Eight-player search must sample, not enumerate future cards")

    game = SampleOnly(horizon=2)
    rng = Random(8)
    worlds = []
    for _ in range(8):
        state = game.initial_state()
        while game.actor(state) == -1:
            action, _ = game.sample_chance_outcome(state, rng)
            state = game.transition(state, action)
        worlds.append(state)
    key = game.public_key(worlds[0])
    assert all(game.public_key(state) == key for state in worlds)
    # Synthetic finite belief exercises the engine; it is not a certified live full range.
    return game, RootDistribution(key, tuple(worlds), (1 / 8,) * 8, 1)


def test_eight_player_800_point_rollouts_complete_two_hands_without_card_enumeration():
    game, root = mushroom_root()
    subgame = DepthLimitedGame(game, root, UniformPolicy(), depth_limit=1, sampled_rollouts=True)
    cutoff = subgame.transition(subgame.wrap_state(root.states[0]), "call")
    for seat in range(8):
        assert subgame.actor(cutoff) == seat
        cutoff = subgame.transition(cutoff, "continue:blueprint")
    assert subgame.actor(cutoff) == -1
    rng = Random(19)
    boards, scores, horizons = set(), set(), set()
    for _ in range(16):
        terminal, payoffs = finish(subgame, cutoff, rng)
        assert terminal.underlying.table.ended
        assert terminal.underlying.table.hand_number in (1, 2)
        horizons.add(terminal.underlying.table.hand_number)
        assert sum(payoffs) == 0 and len(payoffs) == 8
        assert sum(terminal.underlying.table.stacks) == 6400
        boards.add(terminal.underlying.public_history[0])
        scores.add(payoffs)
    assert len(boards) > 1 and len(scores) > 1
    assert 2 in horizons
    assert subgame.rollout_samples == 16


def test_eight_player_search_uses_sampled_tails_and_reports_its_belief_limitation():
    game, root = mushroom_root()
    result = solve_subgame(
        game,
        root.states[0],
        UniformPolicy(),
        root_distribution=root,
        sampled_rollouts=True,
        algorithm="mccfr",
        depth_limit=1,
        iterations=32,
        seed=3,
        max_nodes=500_000,
        max_rollout_nodes=500_000,
    )
    assert result.diagnostics.completed_iterations == 32
    assert result.actions == game.legal_actions(root.states[0])
    assert sum(result.probabilities) == pytest.approx(1)
    report = result.to_dict()
    assert report["continuation_evaluation"] == "sampled_trajectories"
    assert report["rollout_samples"] > 0 and report["rollout_cache_entries"] == 0
    assert report["root_source"] == "caller_supplied_public_belief"
    assert any("no convergence" in warning for warning in report["warnings"])
    assert any("have not been certified" in warning for warning in report["warnings"])


def test_sampled_mode_requires_a_sampling_solver():
    game = KuhnPoker()
    with pytest.raises(ValueError, match="algorithm=mccfr"):
        solve_subgame(game, game.replay((2, 0)), UniformPolicy(), sampled_rollouts=True)


def test_pluribus_blueprint_to_sampled_search_can_be_evaluated_independently():
    game = KuhnPoker()
    trainer = Trainer(game, algorithm="pluribus", seed=42)
    trainer.train(3000)
    result = solve_subgame(
        game,
        game.replay((2, 0)),
        trainer.average_policy(missing="error"),
        depth_limit=1,
        iterations=3000,
        algorithm="mccfr",
        sampled_rollouts=True,
        seed=32,
        max_nodes=1_000_000,
        max_rollout_nodes=1_000_000,
    )
    report = evaluate_strategy(result.subgame, result.average_policy, max_nodes=100_000)
    assert result.blueprint_fallback_queries == 0
    assert report.policy_values == pytest.approx((-1 / 18, 1 / 18), abs=0.03)
    assert report.exploitability < 0.05


def test_sampled_tail_deadline_is_shared_and_no_value_is_returned_after_it():
    subgame, start = kuhn_tail(deadline_seconds=1)
    subgame.rollout_budget.started -= 2
    with pytest.raises(EvaluationLimitError, match="deadline"):
        subgame.sample_chance_outcome(start, Random(0))
    with pytest.raises(EvaluationLimitError, match="deadline"):
        subgame.transition(start, "check")


def test_search_does_not_merge_actual_hands_when_the_blueprint_uses_lossy_buckets():
    class CoarseBlueprintGame:
        def __init__(self):
            self.base = KuhnPoker()

        def __getattr__(self, name):
            return getattr(self.base, name)

        def information_key(self, state, player):
            return f"coarse:{player}:{state.history}"

        def search_information_key(self, state, player):
            return self.base.information_key(state, player)

    model = CoarseBlueprintGame()
    low, high = model.replay((0, 1)), model.replay((2, 1))
    assert model.information_key(low, 0) == model.information_key(high, 0)
    first = solve_subgame(model, low, UniformPolicy(), iterations=300, depth_limit=3)
    second = solve_subgame(model, high, UniformPolicy(), iterations=300, depth_limit=3)
    assert first.information_key == model.base.information_key(low, 0)
    assert second.information_key == model.base.information_key(high, 0)
    assert first.probabilities != second.probabilities
    subgame = first.subgame
    assert subgame.information_key(subgame.wrap_state(low), 0) != subgame.information_key(
        subgame.wrap_state(high), 0
    )
