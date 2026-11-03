"""Public ranges, hidden simultaneous choices, vector tails, and search limits."""

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.evaluation import EvaluationLimitError, best_response, evaluate_strategy
from texas_strategy.policy import TabularPolicy
from texas_strategy.search import (
    CONTINUATION_ACTIONS,
    ContinuationPolicy,
    DepthLimitedGame,
    RootDistribution,
    SearchCoverageError,
    action_semantics,
    build_public_root,
    solve_subgame,
)


class UniformPolicy:
    def probabilities(self, game, state):
        actions = game.legal_actions(state)
        return (1 / len(actions),) * len(actions)


class RankBiasedPolicy(UniformPolicy):
    def probabilities(self, game, state):
        if game.actor(state) == 0 and not state.history:
            bet = (0.1, 0.5, 0.9)[state.cards[0]]
            return 1 - bet, bet
        return super().probabilities(game, state)


def test_public_root_uses_joint_reach_and_keeps_every_hero_hand():
    game = KuhnPoker()
    target = game.replay((2, 0), ("bet",))
    root = build_public_root(game, game.public_key(target), RankBiasedPolicy())
    assert len(root.states) == 6
    assert {state.cards[1] for state in root.states} == {0, 1, 2}
    assert root.reach_probability == pytest.approx(0.5)
    for state, probability in zip(root.states, root.probabilities, strict=True):
        assert probability == pytest.approx((0.1, 0.5, 0.9)[state.cards[0]] / 3)
    other_hand = game.replay((0, 2), ("bet",))
    assert root == build_public_root(game, game.public_key(other_hand), RankBiasedPolicy())


def test_zero_reach_is_rejected_instead_of_inventing_a_uniform_root():
    class AlwaysCheck(UniformPolicy):
        def probabilities(self, game, state):
            return (1.0, 0.0)

    game = KuhnPoker()
    with pytest.raises(SearchCoverageError, match="zero blueprint reach"):
        build_public_root(game, game.public_key(game.replay((0, 1), ("bet",))), AlwaysCheck())


def test_actual_private_information_set_must_have_public_root_support():
    class HighCardOnlyBet(UniformPolicy):
        def probabilities(self, game, state):
            if game.actor(state) == 0 and not state.history:
                return (0.0, 1.0) if state.cards[0] == 2 else (1.0, 0.0)
            return super().probabilities(game, state)

    game = KuhnPoker()
    impossible_hand = game.replay((0, 2), ("bet",))
    with pytest.raises(SearchCoverageError, match="private information set has zero"):
        solve_subgame(game, impossible_hand, HighCardOnlyBet(), iterations=2)


def test_three_player_leaf_choices_include_hero_and_are_hidden_from_later_players():
    game = KuhnPoker(3)
    state = game.replay((3, 1, 0))
    root = build_public_root(game, game.public_key(state), UniformPolicy())
    subgame = DepthLimitedGame(game, root, UniformPolicy(), depth_limit=1)
    cutoff = subgame.transition(subgame.wrap_state(state), "bet")
    assert subgame.actor(cutoff) == 0  # Hero also chooses a continuation.
    assert subgame.legal_actions(cutoff) == CONTINUATION_ACTIONS
    a = subgame.transition(cutoff, "continue:fold")
    b = subgame.transition(cutoff, "continue:raise")
    assert subgame.actor(a) == subgame.actor(b) == 1
    assert subgame.information_key(a, 1) == subgame.information_key(b, 1)
    assert subgame.observation(a, 1) == subgame.observation(b, 1)
    assert subgame.information_key(a, 0) != subgame.information_key(b, 0)
    a = subgame.transition(a, "continue:call")
    b = subgame.transition(b, "continue:blueprint")
    assert subgame.actor(a) == subgame.actor(b) == 2
    assert subgame.information_key(a, 2) == subgame.information_key(b, 2)
    done = subgame.transition(a, "continue:raise")
    assert subgame.actor(done) is None
    payoffs = subgame.terminal_payoffs(done)
    assert len(payoffs) == 3
    assert sum(payoffs) == pytest.approx(0)


def test_folded_player_is_excluded_from_continuation_choices():
    game = KuhnPoker(3)
    state = game.replay((3, 1, 0), ("bet",))
    root = build_public_root(game, game.public_key(state), UniformPolicy())
    subgame = DepthLimitedGame(game, root, UniformPolicy(), depth_limit=1)
    cutoff = subgame.transition(subgame.wrap_state(state), "fold")
    assert subgame.actor(cutoff) == 0
    next_player = subgame.transition(cutoff, "continue:blueprint")
    assert subgame.actor(next_player) == 2
    done = subgame.transition(next_player, "continue:blueprint")
    assert subgame.actor(done) is None
    assert subgame.terminal_payoffs(done)[1] == -1


def test_joint_tail_utility_uses_both_players_choices_and_exact_vector_rollout():
    game = KuhnPoker()
    state = game.replay((2, 0))
    root = build_public_root(game, game.public_key(state), UniformPolicy())
    subgame = DepthLimitedGame(game, root, UniformPolicy(), depth_limit=1)
    cutoff = subgame.transition(subgame.wrap_state(state), "check")

    def terminal(first, second):
        return subgame.transition(
            subgame.transition(cutoff, f"continue:{first}"), f"continue:{second}"
        )

    baseline = terminal("blueprint", "blueprint")
    hero_calls = terminal("call", "blueprint")
    other_bets = terminal("blueprint", "raise")
    # Independent calculation: after check, P(check)=.5; bet leads to fold/call .5 each.
    assert subgame.terminal_payoffs(baseline) == pytest.approx((0.75, -0.75))
    assert subgame.terminal_payoffs(hero_calls) == pytest.approx((1.2, -1.2))
    assert subgame.terminal_payoffs(other_bets) == pytest.approx((0.6, -0.6))
    used = subgame.rollout_budget.nodes
    assert subgame.terminal_payoffs(baseline) == pytest.approx((0.75, -0.75))
    assert subgame.rollout_budget.nodes == used  # deterministic cached joint leaf vector


def test_bias_families_map_kuhn_and_sized_holdem_actions_explicitly():
    assert action_semantics("check") == action_semantics("call") == "call"
    assert action_semantics("bet") == action_semantics("raise_to:25") == "raise"
    game = KuhnPoker()
    state = game.replay((1, 0))
    assert ContinuationPolicy(UniformPolicy(), "call").probabilities(game, state) == (0.8, 0.2)
    assert ContinuationPolicy(UniformPolicy(), "raise").probabilities(game, state) == (0.2, 0.8)


def test_complete_horizon_search_average_agrees_with_full_kuhn_equilibrium_value():
    game = KuhnPoker()
    result = solve_subgame(
        game, game.replay((2, 0)), UniformPolicy(), depth_limit=3, iterations=600
    )
    report = evaluate_strategy(result.subgame, result.average_policy)
    assert report.policy_values == pytest.approx((-1 / 18, 1 / 18), abs=0.01)
    assert report.exploitability < 0.025
    assert result.root_worlds == 6
    assert result.subgame.rollout_cache_entries == 0
    assert result.quality == "conditional_approximate_search"
    assert sum(result.probabilities) == pytest.approx(1)
    assert result.actions == ("check", "bet")


@pytest.mark.parametrize("algorithm", ["cfr", "mccfr"])
def test_eight_player_search_plays_current_regrets_and_preserves_separate_average(algorithm):
    class HiddenChoices:
        num_players = 8

        def initial_state(self):
            return ()

        def actor(self, state):
            return len(state) if len(state) < self.num_players else None

        def legal_actions(self, state):
            return ("fold", "call")

        def public_key(self, state):
            return len(state)

        def information_key(self, state, player):
            return str(player)

        def active_players(self, state):
            return tuple(range(self.num_players))

        def transition(self, state, action):
            return (*state, action)

        def terminal_payoffs(self, state):
            # A known dominant action isolates strategy selection from poker strength.
            scores = [int(action == "call") for action in state]
            mean = sum(scores) / self.num_players
            return tuple(score - mean for score in scores)

    game = HiddenChoices()
    result = solve_subgame(
        game, (), UniformPolicy(), depth_limit=8, iterations=1, algorithm=algorithm, seed=23
    )
    assert result.diagnostics.completed_iterations == 1
    assert result.probabilities == result.policy.probabilities(
        result.subgame, result.subgame.wrap_state(())
    )
    assert result.probabilities == (0.0, 1.0)
    assert len(result.policy.entries) == len(result.average_policy.entries) == 8
    for key, entry in result.policy.entries.items():
        assert entry.probabilities == (0.0, 1.0)
        assert result.average_policy.lookup(*key).probabilities == (0.5, 0.5)
    assert result.to_dict()["action_policy"] == "latest_committed_regret_matching"
    assert result.to_dict()["average_policy"] == "iteration_weighted"


def test_returned_action_only_depends_on_own_information_after_full_range_solve():
    game = KuhnPoker()
    first = solve_subgame(game, game.replay((1, 0)), UniformPolicy(), iterations=10, depth_limit=1)
    second = solve_subgame(game, game.replay((1, 2)), UniformPolicy(), iterations=10, depth_limit=1)
    assert first.root_distribution == second.root_distribution
    assert first.probabilities == second.probabilities
    assert {s.cards[0] for s in first.root_distribution.states} == {0, 1, 2}


def test_all_search_budgets_and_large_game_coverage_are_explicit():
    game = KuhnPoker()
    target = game.replay((2, 0))
    with pytest.raises(EvaluationLimitError, match="hidden-world"):
        solve_subgame(game, target, UniformPolicy(), max_root_worlds=2)
    with pytest.raises(EvaluationLimitError, match="node budget"):
        solve_subgame(game, target, UniformPolicy(), depth_limit=1, max_rollout_nodes=1)
    with pytest.raises(EvaluationLimitError, match="no complete"):
        solve_subgame(game, target, UniformPolicy(), max_nodes=1)

    class LargeKuhn:
        supports_exact_enumeration = False

        def public_key(self, state):
            return "decision"

        def initial_state(self):
            pytest.fail("large game must reject before chance enumeration")

    with pytest.raises(SearchCoverageError, match="bounded public root"):
        build_public_root(LargeKuhn(), "decision", UniformPolicy())
    root = build_public_root(game, game.public_key(target), UniformPolicy())
    supplied = solve_subgame(game, target, UniformPolicy(), root_distribution=root, iterations=2)
    assert supplied.to_dict()["root_source"] == "caller_supplied_public_belief"
    with pytest.raises(ValueError, match="sum to one"):
        RootDistribution(root.public_key, root.states, (0.1,) * 6, 1)


def test_unknown_blueprint_coverage_is_strict_unless_fallback_is_explicit():
    game = KuhnPoker()
    state = game.replay((2, 0))
    with pytest.raises(SearchCoverageError, match="blueprint does not cover"):
        solve_subgame(game, state, TabularPolicy(), iterations=2, depth_limit=1)
    result = solve_subgame(
        game, state, TabularPolicy(), iterations=2, depth_limit=1, allow_uniform_fallback=True
    )
    assert result.blueprint_fallback_queries > 0
    assert "uniform fallbacks" in result.to_dict()["warnings"][-1]


def test_finite_continuations_cannot_guarantee_improvement_against_a_true_best_response():
    class TrapGame:
        num_players = 2

        def initial_state(self):
            return ()

        def actor(self, state):
            return 0 if not state else (1 if state == ("bet",) else None)

        def legal_actions(self, state):
            return ("check", "bet") if not state else ("fold", "call")

        def information_key(self, state, player):
            return f"{state}:{player}"

        def public_key(self, state):
            return state

        def active_players(self, state):
            return (0, 1)

        def transition(self, state, action):
            return (*state, action)

        def terminal_payoffs(self, state):
            reward = {("check",): 1, ("bet", "fold"): 2, ("bet", "call"): -10}[state]
            return reward, -reward

    class SafeButNaive:
        def probabilities(self, game, state):
            return (1.0, 0.0)  # safe root action; opponent continuation always folds

    game = TrapGame()
    blueprint = SafeButNaive()
    result = solve_subgame(game, (), blueprint, depth_limit=1, iterations=30)
    assert result.probabilities[1] > 0.99

    class SearchedPolicy(SafeButNaive):
        def probabilities(self, game, state):
            return result.probabilities if not state else super().probabilities(game, state)

    baseline_worst = -best_response(game, blueprint, 1).value
    searched_worst = -best_response(game, SearchedPolicy(), 1).value
    assert baseline_worst == 1
    assert searched_worst < -9
    assert "do not guarantee" in result.warnings[0]
