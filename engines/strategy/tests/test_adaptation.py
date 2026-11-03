"""Exact model gains and independent worst-case defenses on conventional Kuhn."""

from itertools import product

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.adaptation import adapt_strategy
from texas_strategy.evaluation import best_response, evaluate_policy
from texas_strategy.policy import TabularPolicy


class KuhnEquilibrium:
    def probabilities(self, game, state):
        rank = state.cards[game.actor(state)]
        if state.history in ((), ("check",)):
            second = (1 / 3, 0, 1)[rank]
        elif state.history == ("bet",):
            second = (0, 1 / 3, 1)[rank]
        else:
            second = (0, 2 / 3, 1)[rank]
        return 1 - second, second


class CallingStation:
    def probabilities(self, game, state):
        # Passively checks but calls every bet, regardless of own rank.
        return (1.0, 0.0) if "check" in game.legal_actions(state) else (0.0, 1.0)


class AggressiveCaller:
    def probabilities(self, game, state):
        # Bets whenever checked to, and calls every bet.
        return 0.0, 1.0


@pytest.mark.parametrize(
    ("model", "weight", "baseline_value", "adapted_value", "gain"),
    (
        (CallingStation(), 0.1, 2 / 9, 7 / 30, 1 / 90),
        (AggressiveCaller(), 0.25, 1 / 9, 1 / 6, 1 / 18),
    ),
)
def test_two_distinct_opponent_models_gain_value_within_exact_worst_case_floor(
    model, weight, baseline_value, adapted_value, gain
):
    game = KuhnPoker()
    baseline = KuhnEquilibrium()
    result = adapt_strategy(
        game,
        baseline,
        model,
        sample_count=200,
        max_worst_case_loss=0.04,
        model_version="synthetic-test-v1",
    )
    assert result.accepted
    assert result.selected_weight == weight
    assert result.baseline_model_value == pytest.approx(baseline_value)
    assert result.candidate_model_value == pytest.approx(adapted_value)
    assert result.model_gain == pytest.approx(gain)
    assert result.baseline_worst_case == pytest.approx(-1 / 18)
    assert result.candidate_worst_case == pytest.approx(-1 / 12)
    assert result.candidate_worst_case >= result.baseline_worst_case - 0.04
    assert evaluate_policy(game, (result.policy, model))[0] == pytest.approx(adapted_value)
    assert -best_response(game, result.policy, 1).value == pytest.approx(
        result.candidate_worst_case
    )
    assert result.to_dict()["model_version"] == "synthetic-test-v1"
    assert result.to_dict()["model_source"] == "caller_supplied_behavioral_profile"


def test_model_gain_alone_does_not_pass_zero_worst_case_loss_guard():
    baseline = KuhnEquilibrium()
    result = adapt_strategy(
        KuhnPoker(), baseline, CallingStation(), sample_count=200, max_worst_case_loss=0
    )
    assert all(candidate.model_gain > 0 for candidate in result.candidates)
    assert all(not candidate.passes_guard for candidate in result.candidates)
    assert not result.accepted
    assert result.reason == "worst_case_guard_rejected"
    assert result.policy is baseline
    assert result.selected_weight == result.model_gain == 0


def test_misclassified_opponent_cannot_break_reported_floor_for_any_pure_kuhn_response():
    game = KuhnPoker()
    baseline = KuhnEquilibrium()
    result = adapt_strategy(
        game, baseline, CallingStation(), sample_count=200, max_worst_case_loss=0.04
    )
    opponent_infosets = {}

    def collect(state):
        actor = game.actor(state)
        if actor is None:
            return
        if actor == 1:
            opponent_infosets[game.information_key(state, 1)] = game.legal_actions(state)
        for action in game.legal_actions(state):
            collect(game.transition(state, action))

    collect(game.initial_state())

    class ActualOpponent:
        def __init__(self, choices):
            self.choices = dict(zip(opponent_infosets, choices, strict=True))

        def probabilities(self, game, state):
            chosen = self.choices[game.information_key(state, 1)]
            return tuple(float(action == chosen) for action in game.legal_actions(state))

    # This independently enumerates all 64 pure opponent policies, including
    # ones unrelated to the modeled calling station and adversarial to hero.
    actual_values = [
        evaluate_policy(game, (result.policy, ActualOpponent(choices)))[0]
        for choices in product(*opponent_infosets.values())
    ]
    assert len(actual_values) == 64
    assert min(actual_values) == pytest.approx(result.candidate_worst_case)
    assert min(actual_values) >= result.baseline_worst_case - result.max_worst_case_loss
    assert min(actual_values) < result.baseline_worst_case  # finite tolerance is real risk


def test_mixture_keeps_legal_full_range_information_sets_and_only_changes_hero():
    game = KuhnPoker()
    baseline = KuhnEquilibrium()
    result = adapt_strategy(game, baseline, AggressiveCaller(), sample_count=200)
    seen = {}

    def visit(state):
        actor = game.actor(state)
        if actor is None:
            return
        if actor >= 0:
            probabilities = result.policy.probabilities(game, state)
            assert len(probabilities) == len(game.legal_actions(state))
            assert sum(probabilities) == pytest.approx(1)
            assert all(0 <= p <= 1 for p in probabilities)
            key = (actor, game.information_key(state, actor))
            assert seen.setdefault(key, probabilities) == probabilities
            if actor != result.hero:
                assert probabilities == baseline.probabilities(game, state)
        for action in game.legal_actions(state):
            visit(game.transition(state, action))

    visit(game.initial_state())
    assert len(seen) == 12


def test_low_sample_and_multiplayer_paths_preserve_baseline_without_evaluating_model():
    class UnqueriedModel:
        def probabilities(self, game, state):
            pytest.fail("gated model must not be queried")

    baseline = KuhnEquilibrium()
    sparse = adapt_strategy(
        KuhnPoker(), baseline, UnqueriedModel(), sample_count=49, min_samples=50
    )
    assert sparse.policy is baseline
    assert sparse.reason == "insufficient_samples"
    assert sparse.nodes == 0
    assert sparse.baseline_worst_case is None
    multi = adapt_strategy(KuhnPoker(3), baseline, UnqueriedModel(), sample_count=200)
    assert multi.policy is baseline
    assert multi.reason == "multiplayer_guard_unsupported"
    assert multi.nodes == 0


def test_shared_budget_discards_incomplete_candidate_selection_even_after_an_early_pass():
    baseline = KuhnEquilibrium()
    result = adapt_strategy(
        KuhnPoker(), baseline, AggressiveCaller(), sample_count=200, max_nodes=300
    )
    assert result.reason == "budget_exhausted"
    assert result.policy is baseline
    assert result.nodes == 300
    assert result.candidates and result.candidates[0].passes_guard
    assert result.selected_weight == 0
    deadline = adapt_strategy(
        KuhnPoker(), baseline, CallingStation(), sample_count=200, deadline_seconds=1e-15
    )
    assert deadline.reason == "budget_exhausted"
    assert deadline.policy is baseline


def test_equilibrium_model_has_no_positive_gain_and_hero_model_entry_is_unused():
    class UnqueriedHeroModel:
        def probabilities(self, game, state):
            pytest.fail("hero's supplied opponent-model entry must not be used")

    baseline = KuhnEquilibrium()
    result = adapt_strategy(
        KuhnPoker(), baseline, (UnqueriedHeroModel(), baseline), sample_count=200
    )
    assert result.policy is baseline
    assert result.reason == "no_positive_model_gain"
    assert all(abs(candidate.model_gain) < 1e-12 for candidate in result.candidates)


def test_non_zero_sum_and_incomplete_policy_coverage_do_not_claim_guarded_adaptation():
    class GeneralSumKuhn(KuhnPoker):
        def terminal_payoffs(self, state):
            a, b = super().terminal_payoffs(state)
            return a + 0.1, b

    baseline = KuhnEquilibrium()
    nonzero = adapt_strategy(GeneralSumKuhn(), baseline, CallingStation(), sample_count=200)
    assert nonzero.policy is baseline
    assert nonzero.reason == "non_zero_sum_guard_unsupported"
    incomplete = TabularPolicy()
    missing = adapt_strategy(KuhnPoker(), incomplete, CallingStation(), sample_count=200)
    assert missing.policy is incomplete
    assert missing.reason == "incomplete_policy_coverage"
    assert incomplete.fallback_count == 0


def test_clairvoyant_opponent_model_is_rejected():
    class CheatingModel:
        def probabilities(self, game, state):
            return (1.0, 0.0) if state.cards[0] > state.cards[1] else (0.0, 1.0)

    baseline = KuhnEquilibrium()
    result = adapt_strategy(KuhnPoker(), baseline, CheatingModel(), sample_count=200)
    assert result.policy is baseline
    assert result.reason == "invalid_model_or_baseline"


@pytest.mark.parametrize("weights", ((0.26,), (0.0,), (), (float("nan"),)))
def test_adaptation_rejects_invalid_or_excessive_mixing_weights(weights):
    with pytest.raises(ValueError, match="mixing_weights"):
        adapt_strategy(KuhnPoker(), KuhnEquilibrium(), CallingStation(), mixing_weights=weights)
