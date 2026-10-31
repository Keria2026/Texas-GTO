"""Independent values and responses, including a hidden-state guessing counterexample."""

from itertools import product

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.evaluation import (
    EvaluationLimitError,
    best_response,
    compare_policies,
    evaluate_policy,
    evaluate_strategy,
)


class UniformPolicy:
    def probabilities(self, game, state):
        actions = game.legal_actions(state)
        return (1 / len(actions),) * len(actions)


class KuhnEquilibrium:
    """Conventional Kuhn equilibrium with first-seat bluff parameter alpha=1/3."""

    def probabilities(self, game, state):
        player = game.actor(state)
        rank = state.cards[player]
        if state.history == ():
            aggressive = (1 / 3, 0, 1)[rank]
        elif state.history == ("check",):
            aggressive = (1 / 3, 0, 1)[rank]
        elif state.history == ("bet",):
            aggressive = (0, 1 / 3, 1)[rank]
        else:
            aggressive = (0, 2 / 3, 1)[rank]
        return 1 - aggressive, aggressive


class HiddenGuess:
    """Player zero cannot see a biased coin; an omniscient max incorrectly earns one."""

    num_players = 2

    def initial_state(self):
        return ()

    def actor(self, state):
        return -1 if not state else (0 if len(state) == 1 else None)

    def chance_outcomes(self, state):
        return (("heads", 0.9), ("tails", 0.1))

    def legal_actions(self, state):
        return ("heads", "tails")

    def information_key(self, state, player):
        return "unobserved-coin"

    def transition(self, state, action):
        return (*state, action)

    def terminal_payoffs(self, state):
        reward = float(state[0] == state[1])
        return reward, -reward


def test_known_kuhn_equilibrium_has_value_minus_one_eighteenth_and_zero_exploitability():
    report = evaluate_strategy(KuhnPoker(), KuhnEquilibrium())
    assert report.policy_values == pytest.approx((-1 / 18, 1 / 18))
    assert report.best_response_values == pytest.approx(report.policy_values)
    assert report.nash_conv == pytest.approx(0, abs=1e-14)
    assert report.exploitability == pytest.approx(0, abs=1e-14)


def test_hidden_coin_response_chooses_one_action_for_both_hidden_worlds():
    response = best_response(HiddenGuess(), UniformPolicy(), 0)
    assert response.value == pytest.approx(0.9)
    assert response.actions == {"unobserved-coin": "heads"}
    assert evaluate_policy(HiddenGuess(), UniformPolicy()) == pytest.approx((0.5, -0.5))


@pytest.mark.parametrize("player", (0, 1))
def test_kuhn_best_response_matches_exhaustive_pure_information_set_strategies(player):
    game = KuhnPoker()
    infosets = {}

    def collect(state):
        actor = game.actor(state)
        if actor is None:
            return
        if actor == player:
            infosets[game.information_key(state, player)] = tuple(game.legal_actions(state))
        for action in game.legal_actions(state):
            collect(game.transition(state, action))

    collect(game.initial_state())
    assert len(infosets) == 6

    class PureResponse:
        def __init__(self, choices):
            self.choices = dict(zip(infosets, choices, strict=True))

        def probabilities(self, game, state):
            action = self.choices[game.information_key(state, player)]
            return tuple(float(a == action) for a in game.legal_actions(state))

    candidates = []
    for choices in product(*infosets.values()):
        profile = [UniformPolicy(), UniformPolicy()]
        profile[player] = PureResponse(choices)
        candidates.append(evaluate_policy(game, profile)[player])
    assert best_response(game, UniformPolicy(), player).value == pytest.approx(max(candidates))


def test_uniform_kuhn_known_values_and_multiplayer_metric_labels():
    report = evaluate_strategy(KuhnPoker(), UniformPolicy())
    assert report.policy_values == pytest.approx((1 / 8, -1 / 8))
    assert report.best_response_values == pytest.approx((1 / 2, 5 / 12))
    assert report.nash_conv == pytest.approx(11 / 12)
    assert report.exploitability == pytest.approx(11 / 24)
    multiplayer = evaluate_strategy(KuhnPoker(3), UniformPolicy())
    assert sum(multiplayer.policy_values) == pytest.approx(0)
    assert multiplayer.nash_conv > 0
    assert multiplayer.is_zero_sum
    assert multiplayer.exploitability is None


def test_general_sum_game_does_not_report_two_player_exploitability():
    class CooperativeGuess(HiddenGuess):
        def terminal_payoffs(self, state):
            reward = float(state[0] == state[1])
            return reward, reward

    report = evaluate_strategy(CooperativeGuess(), UniformPolicy())
    assert not report.is_zero_sum
    assert report.nash_conv == pytest.approx(0.4)
    assert report.exploitability is None


def test_rejects_clairvoyant_policy_and_imperfect_recall_game():
    class CheatingPolicy:
        def probabilities(self, game, state):
            return (1.0, 0.0) if state[0] == "heads" else (0.0, 1.0)

    with pytest.raises(ValueError, match="hidden information"):
        evaluate_strategy(HiddenGuess(), CheatingPolicy())

    class ForgetfulGame(HiddenGuess):
        def actor(self, state):
            return 0 if len(state) < 2 else None

    with pytest.raises(ValueError, match="perfect recall"):
        best_response(ForgetfulGame(), UniformPolicy(), 0)


def test_exact_budgets_fail_without_publishing_a_partial_value():
    with pytest.raises(EvaluationLimitError, match="node budget"):
        evaluate_strategy(KuhnPoker(), UniformPolicy(), max_nodes=3)
    with pytest.raises(EvaluationLimitError, match="node budget"):
        evaluate_policy(KuhnPoker(), UniformPolicy(), max_nodes=3)
    with pytest.raises(EvaluationLimitError, match="deadline"):
        best_response(KuhnPoker(), UniformPolicy(), 0, deadline_seconds=1e-15)

    class LargeGame(HiddenGuess):
        supports_exact_enumeration = False

        def chance_outcomes(self, state):
            pytest.fail("large exact chance trees must not be requested")

    with pytest.raises(EvaluationLimitError, match="full-tree"):
        evaluate_strategy(LargeGame(), UniformPolicy())


def test_paired_comparison_is_seeded_and_identical_policies_have_zero_standard_error():
    game = KuhnPoker()
    report = compare_policies(game, UniformPolicy(), UniformPolicy(), samples=200, seed=123)
    assert report.gain == 0
    assert report.standard_error == 0
    assert report == compare_policies(game, UniformPolicy(), UniformPolicy(), samples=200, seed=123)
    candidate = compare_policies(game, UniformPolicy(), KuhnEquilibrium(), samples=2_000, seed=71)
    exact_base = evaluate_policy(game, UniformPolicy())[0]
    exact_candidate = evaluate_policy(game, (KuhnEquilibrium(), UniformPolicy()))[0]
    assert candidate.gain == pytest.approx(
        exact_candidate - exact_base, abs=4 * candidate.standard_error
    )


def test_sampled_comparison_uses_direct_chance_sampling_when_provided():
    class SampledGuess(HiddenGuess):
        supports_exact_enumeration = False

        def sample_chance(self, state, rng):
            return "heads" if rng.random() < 0.9 else "tails"

        def chance_outcomes(self, state):
            pytest.fail("sampling must not enumerate this chance tree")

    report = compare_policies(SampledGuess(), UniformPolicy(), UniformPolicy(), samples=20)
    assert report.gain == 0
