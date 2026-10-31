from dataclasses import replace
from math import exp

import pytest

from texas_strategy.opponent import (
    OpponentModel,
    ProfileEvent,
    ScopedPlayer,
    bayesian_range_update,
    blend_experts,
)

PLAYER = ScopedPlayer("session", "table", "player")
CONTEXT = "preflop:button:unopened"


def opportunity(event_id, observed_at=1, success=False, eligible=True, **kwargs):
    return ProfileEvent(
        event_id,
        PLAYER,
        CONTEXT,
        observed_at,
        "opportunity",
        name="pfr",
        success=success,
        eligible=eligible,
        **kwargs,
    )


def action(event_id, choice, support=("fold", "call", "raise"), at=1):
    return ProfileEvent(
        event_id, PLAYER, CONTEXT, at, "action", action=choice, legal_actions=support
    )


def test_true_opportunity_denominator_and_beta_shrinkage():
    model = OpponentModel(
        [
            opportunity("win", success=True),
            opportunity("no"),
            opportunity("not-an-opportunity", eligible=False),
        ]
    )
    stat = model.stat(PLAYER, CONTEXT, "pfr", as_of=1, prior_alpha=2, prior_beta=8)
    assert stat.successes == 1
    assert stat.opportunities == 2
    assert stat.alpha == 3
    assert stat.beta == 9
    assert stat.mean == 0.25
    lo, hi = stat.interval()
    assert 0 <= lo <= stat.mean <= hi <= 1
    # Other statistics/contexts and unobserved opportunities do not add failures.
    assert model.stat(PLAYER, CONTEXT, "three_bet", as_of=1).opportunities == 0
    assert model.stat(PLAYER, "another-node", "pfr", as_of=1).opportunities == 0
    with pytest.raises(ValueError, match="eligible opportunity"):
        opportunity("invalid", success=True, eligible=False)


def test_session_scope_and_observation_availability_prevent_time_leakage():
    model = OpponentModel(
        [
            opportunity("early", observed_at=1, success=False),
            opportunity("hand-finalized", observed_at=2, available_at=10, success=True),
            opportunity("future", observed_at=20, success=True),
            replace(opportunity("other"), player=ScopedPlayer("another", "table", "player")),
        ]
    )
    assert model.stat(PLAYER, CONTEXT, "pfr", as_of=3).opportunities == 1
    at_ten = model.stat(PLAYER, CONTEXT, "pfr", as_of=10)
    assert (at_ten.successes, at_ten.opportunities) == (1, 2)
    recent = model.stat(PLAYER, CONTEXT, "pfr", as_of=10, since=2)
    assert (recent.successes, recent.opportunities) == (1, 1)
    assert (recent.window_start, recent.as_of) == (2, 10)
    assert recent.interval_method == "chebyshev_posterior_mass_bound"
    with pytest.raises(ValueError, match="cannot precede"):
        opportunity("backdated", observed_at=10, available_at=9)


def test_deduplication_and_atomic_correction_rebuild():
    event = opportunity("a", success=True)
    model = OpponentModel([event])
    assert not model.record(event)
    with pytest.raises(ValueError, match="Conflicting payload"):
        model.record(replace(event, success=False))
    assert model.stat(PLAYER, CONTEXT, "pfr", as_of=1).successes == 1
    with pytest.raises(ValueError, match="Conflicting payload"):
        model.rebuild([event, replace(event, success=False)])
    assert model.events == (event,)
    corrected = replace(event, success=False)
    model.rebuild([corrected, corrected])
    stat = model.stat(PLAYER, CONTEXT, "pfr", as_of=1)
    assert (stat.successes, stat.opportunities) == (0, 1)


def test_action_likelihood_is_dirichlet_shrunk_and_support_conditioned():
    model = OpponentModel(
        [
            action("raise-1", "raise"),
            action("raise-2", "raise", support=("raise", "call", "fold")),
            action("call", "call"),
            action("different-opportunity", "check", support=("check", "bet")),
            action("future", "fold", at=100),
        ]
    )
    posterior = model.actions(PLAYER, CONTEXT, ("fold", "call", "raise"), as_of=5)
    assert posterior.observations == 3
    assert posterior.counts == {"call": 1, "fold": 0, "raise": 2}
    assert posterior.probabilities == pytest.approx({"call": 2 / 6, "fold": 1 / 6, "raise": 3 / 6})
    prior = {"call": 2, "fold": 6, "raise": 2}
    new_player = ScopedPlayer("session", "table", "unknown")
    missing = model.actions(new_player, CONTEXT, tuple(prior), as_of=5, prior=prior)
    assert missing.probabilities == {"call": 0.2, "fold": 0.6, "raise": 0.2}
    assert missing.observations == 0
    with pytest.raises(ValueError, match="legal"):
        action("illegal", "check")


def test_range_updates_use_action_likelihood_and_card_blockers():
    hands = {("As", "Ah"): 0.2, ("Kd", "Kh"): 0.5, ("Qc", "Qd"): 0.3}
    likelihoods = {("Ah", "As"): 1.0, ("Kd", "Kh"): 0.2, ("Qd", "Qc"): 0.8}
    posterior = bayesian_range_update(hands, likelihoods, known_cards=("As", "2h", "3d"))
    assert ("Ah", "As") not in posterior
    assert posterior == pytest.approx({("Kd", "Kh"): 0.1 / 0.34, ("Qc", "Qd"): 0.24 / 0.34})
    assert sum(posterior.values()) == pytest.approx(1)


def test_range_underflow_is_handled_without_losing_all_posterior_mass():
    result = bayesian_range_update(
        {("As", "Ah"): 1e-300, ("Kd", "Kh"): 2e-300},
        {("As", "Ah"): 1e-300, ("Kd", "Kh"): 1e-300},
    )
    assert result == pytest.approx({("Ah", "As"): 1 / 3, ("Kd", "Kh"): 2 / 3})


def test_range_impossible_evidence_missing_likelihood_and_duplicate_cards_are_rejected():
    prior = {("As", "Ah"): 1.0}
    with pytest.raises(ValueError, match="No possible range"):
        bayesian_range_update(prior, {("As", "Ah"): 0.0})
    with pytest.raises(ValueError, match="No possible range"):
        bayesian_range_update(prior, {}, known_cards=("As",))
    with pytest.raises(ValueError, match="Missing action likelihood"):
        bayesian_range_update(prior, {})
    with pytest.raises(ValueError, match="duplicates"):
        bayesian_range_update(prior, prior, ("As", "As"))
    with pytest.raises(ValueError, match="Duplicate unordered"):
        bayesian_range_update({("As", "Ah"): 1, ("Ah", "As"): 1}, prior)
    with pytest.raises(ValueError, match="distinct"):
        bayesian_range_update({("As", "As"): 1}, {})


def test_negative_distance_weights_decrease_and_mixture_is_capped_without_ev_claim():
    base = {"call": 0.5, "fold": 0.5}
    experts = {"near": {"call": 1, "fold": 0}, "far": {"call": 0, "fold": 1}}
    blended = blend_experts(base, experts, {"near": 0, "far": 2}, confidence=0.9, sample_count=30)
    expected_near = 1 / (1 + exp(-2))
    assert blended.expert_weights["near"] == pytest.approx(expected_near)
    assert blended.expert_weights["near"] > blended.expert_weights["far"]
    assert blended.adaptation_weight == 0.25
    assert blended.probabilities["call"] == pytest.approx(0.75 * 0.5 + 0.25 * expected_near)
    assert sum(blended.probabilities.values()) == pytest.approx(1)
    assert not blended.equilibrium_guarantee


def test_low_data_zero_confidence_or_missing_paths_return_baseline():
    base = {"call": 0.5, "fold": 0.5}
    experts = {"expert": {"call": 1, "fold": 0}}
    for confidence, samples, distance in [(1, 0, 0), (0, 100, 0), (1, 100, None)]:
        result = blend_experts(
            base,
            experts,
            {"expert": distance},
            confidence=confidence,
            sample_count=samples,
        )
        assert result.probabilities == base
        assert result.adaptation_weight == 0
        assert result.source == "baseline"


def test_expert_action_alignment_and_finite_normalized_distributions_are_enforced():
    base = {"call": 0.5, "fold": 0.5}
    with pytest.raises(ValueError, match="support"):
        blend_experts(base, {"bad": {"check": 1}}, {"bad": 0}, confidence=1)
    with pytest.raises(ValueError, match="sum to one"):
        blend_experts({"call": 0.4, "fold": 0.4}, {}, {}, confidence=1)
    with pytest.raises(ValueError, match="finite"):
        blend_experts(base, {"bad": base}, {"bad": float("nan")}, confidence=1)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 0])
def test_invalid_prior_concentration_is_rejected(bad):
    model = OpponentModel()
    with pytest.raises(ValueError):
        model.stat(PLAYER, CONTEXT, "pfr", as_of=1, prior_alpha=bad)
    with pytest.raises(ValueError):
        model.actions(PLAYER, CONTEXT, ["call"], as_of=1, prior={"call": bad})
