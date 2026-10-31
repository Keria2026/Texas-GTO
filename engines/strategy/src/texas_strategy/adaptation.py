"""Model-based adaptation on top of a separately usable baseline strategy.

The opponent model is an explicitly supplied behavioral policy, not a range
inferred from VPIP/PFR. For finite two-player zero-sum games, a full-information-
set best response supplies candidate hero actions. Small behavioral mixtures
must improve independently evaluated model value AND retain a measured worst-
case floor relative to the baseline. This is an engineering extension beyond
Pluribus, and a statement about this exact game rather than real-world model
accuracy. Unsupported, sparse, or interrupted calculations preserve baseline.
"""

from __future__ import annotations

import math
import time
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from typing import Any

from texas_strategy.evaluation import (
    BestResponse,
    EvaluationLimitError,
    TraversalBudget,
    best_response,
    checked_probabilities,
    evaluate_policy,
    evaluate_strategy,
    policy_for_player,
)


class _CoverageError(ValueError):
    pass


class _BudgetedGame:
    """Share one expansion budget across all independent evaluator invocations.

    Each evaluator visits the root via initial_state and reaches every other
    tree node through transition. Counting these calls keeps actual attempted
    expansions even when an evaluator raises before returning its diagnostics.
    """

    def __init__(self, game: Any, budget: TraversalBudget):
        self.game = game
        self.budget = budget

    def __getattr__(self, name: str) -> Any:
        return getattr(self.game, name)

    def initial_state(self) -> Any:
        self.budget.tick()
        return self.game.initial_state()

    def transition(self, state: Any, action: Hashable) -> Any:
        self.budget.tick()
        return self.game.transition(state, action)


@dataclass(frozen=True)
class _CoveredProfile:
    profile: Any

    def probabilities(self, game: Any, state: Any) -> tuple[float, ...]:
        policy = policy_for_player(self.profile, game.num_players, game.actor(state))
        if callable(getattr(policy, "contains", None)) and not policy.contains(game, state):
            raise _CoverageError("adaptation requires covered baseline and model information sets")
        return checked_probabilities(game, state, policy)


@dataclass(frozen=True)
class HeroMixturePolicy:
    """One behavioral mixture at every hero information set; other seats are unchanged.

    Behavioral node mixing is not claimed to preserve equilibrium. The resulting
    complete policy is evaluated independently, including the opponent's exact
    information-set best response, before the adapter accepts it.
    """

    baseline: Any
    response: BestResponse
    hero: int
    weight: float

    def probabilities(self, game: Any, state: Any) -> tuple[float, ...]:
        base = checked_probabilities(game, state, self.baseline)
        actor = game.actor(state)
        if actor != self.hero:
            return base
        key = game.information_key(state, self.hero)
        if key not in self.response.actions:
            raise _CoverageError("best-response policy does not cover this hero information set")
        chosen = self.response.actions[key]
        actions = tuple(game.legal_actions(state))
        if chosen not in actions:
            raise ValueError("best-response action is illegal in the requested information set")
        return tuple(
            (1 - self.weight) * probability + self.weight * (action == chosen)
            for action, probability in zip(actions, base, strict=True)
        )


@dataclass(frozen=True)
class CandidateAssessment:
    weight: float
    model_value: float
    model_gain: float
    worst_case_value: float
    passes_guard: bool

    def to_dict(self) -> dict:
        return {
            "weight": self.weight,
            "model_value": self.model_value,
            "model_gain": self.model_gain,
            "worst_case_value": self.worst_case_value,
            "passes_guard": self.passes_guard,
        }


@dataclass(frozen=True)
class AdaptationResult:
    policy: Any = field(repr=False)
    hero: int
    accepted: bool
    reason: str
    selected_weight: float
    model_gain: float
    baseline_model_value: float | None
    candidate_model_value: float | None
    baseline_worst_case: float | None
    candidate_worst_case: float | None
    max_worst_case_loss: float
    sample_count: int
    min_samples: int
    candidates: tuple[CandidateAssessment, ...]
    nodes: int
    elapsed_seconds: float
    quality: str
    model_version: str | None = None
    model_source: str = "caller_supplied_behavioral_profile"
    warnings: tuple[str, ...] = (
        "Opponent model accuracy is unproven; sample counts are a gate, not calibrated confidence.",
        "The floor applies only to this finite two-player zero-sum game and its payoff units.",
        "A floor relative to baseline does not guarantee an improvement against every opponent.",
    )

    def to_dict(self) -> dict:
        return {
            "hero": self.hero,
            "accepted": self.accepted,
            "reason": self.reason,
            "selected_weight": self.selected_weight,
            "model_gain": self.model_gain,
            "baseline_model_value": self.baseline_model_value,
            "candidate_model_value": self.candidate_model_value,
            "baseline_worst_case": self.baseline_worst_case,
            "candidate_worst_case": self.candidate_worst_case,
            "max_worst_case_loss": self.max_worst_case_loss,
            "sample_count": self.sample_count,
            "min_samples": self.min_samples,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "nodes": self.nodes,
            "elapsed_seconds": self.elapsed_seconds,
            "quality": self.quality,
            "model_version": self.model_version,
            "model_source": self.model_source,
            "warnings": list(self.warnings),
        }


def adapt_strategy(
    game: Any,
    baseline: Any,
    opponent_profile: Any,
    *,
    hero: int = 0,
    max_worst_case_loss: float = 0.05,
    mixing_weights: Sequence[float] = (0.05, 0.1, 0.25),
    sample_count: int = 0,
    min_samples: int = 50,
    min_model_gain: float = 1e-9,
    max_nodes: int = 250_000,
    deadline_seconds: float | None = None,
    model_version: str | None = None,
) -> AdaptationResult:
    """Accept a guarded model adaptation or return the exact original baseline object.

    A single policy or one policy per seat can be supplied for each profile.
    Only non-hero seats of ``opponent_profile`` are used. Model gain is the net
    expected payoff over the *whole* game, not card equity or one node's value.
    Weights above 0.25 are rejected, and every candidate is independently tested.
    A single shared node/time budget covers baseline assessment, model response,
    model values and all worst-case checks. Interruption returns baseline even
    when an earlier candidate passed, with partial diagnostics clearly retained.
    """
    started = time.monotonic()
    if type(hero) is not int or not 0 <= hero < game.num_players:
        raise ValueError("hero must name an existing player")
    if not math.isfinite(max_worst_case_loss) or max_worst_case_loss < 0:
        raise ValueError("max_worst_case_loss must be finite and nonnegative")
    if not math.isfinite(min_model_gain) or min_model_gain < 0:
        raise ValueError("min_model_gain must be finite and nonnegative")
    if type(sample_count) is not int or sample_count < 0:
        raise ValueError("sample_count must be a nonnegative integer")
    if type(min_samples) is not int or min_samples < 1:
        raise ValueError("min_samples must be a positive integer")
    weights = tuple(sorted(set(float(weight) for weight in mixing_weights)))
    if not weights or any(
        not math.isfinite(weight) or not 0 < weight <= 0.25 for weight in weights
    ):
        raise ValueError("mixing_weights must be nonempty with every weight in (0, 0.25]")
    budget = TraversalBudget(max_nodes, deadline_seconds)
    bounded_game = _BudgetedGame(game, budget)
    base_value: float | None = None
    base_worst: float | None = None
    candidates: list[CandidateAssessment] = []

    def remaining() -> float | None:
        budget.check_time()
        if deadline_seconds is None:
            return None
        seconds = deadline_seconds - (time.monotonic() - budget.started)
        if seconds <= 0:
            raise EvaluationLimitError("adaptation deadline exceeded")
        return seconds

    def fallback(reason: str) -> AdaptationResult:
        return AdaptationResult(
            baseline,
            hero,
            False,
            reason,
            0.0,
            0.0,
            base_value,
            base_value,
            base_worst,
            base_worst,
            max_worst_case_loss,
            sample_count,
            min_samples,
            tuple(candidates),
            budget.nodes,
            time.monotonic() - started,
            "baseline_fallback",
            model_version,
        )

    if sample_count < min_samples:
        return fallback("insufficient_samples")
    if game.num_players != 2:
        return fallback("multiplayer_guard_unsupported")
    if getattr(game, "supports_exact_enumeration", True) is False:
        return fallback("exact_guard_unsupported")

    try:
        covered_base = _CoveredProfile(baseline)
        model_profile = tuple(
            covered_base
            if player == hero
            else _CoveredProfile(policy_for_player(opponent_profile, game.num_players, player))
            for player in range(game.num_players)
        )
        baseline_report = evaluate_strategy(
            bounded_game, covered_base, max_nodes=max_nodes, deadline_seconds=remaining()
        )
        if not baseline_report.is_zero_sum:
            return fallback("non_zero_sum_guard_unsupported")
        opponent = 1 - hero
        base_worst = -baseline_report.best_response_values[opponent]
        response = best_response(
            bounded_game, model_profile, hero, max_nodes=max_nodes, deadline_seconds=remaining()
        )
        base_value = evaluate_policy(
            bounded_game, model_profile, max_nodes=max_nodes, deadline_seconds=remaining()
        )[hero]
        selected: CandidateAssessment | None = None
        selected_policy: HeroMixturePolicy | None = None
        for weight in weights:
            policy = HeroMixturePolicy(covered_base, response, hero, weight)
            candidate_model = list(model_profile)
            candidate_model[hero] = policy
            value = evaluate_policy(
                bounded_game, candidate_model, max_nodes=max_nodes, deadline_seconds=remaining()
            )[hero]
            worst = -best_response(
                bounded_game, policy, opponent, max_nodes=max_nodes, deadline_seconds=remaining()
            ).value
            passes_guard = worst >= base_worst - max_worst_case_loss - 1e-10
            candidate = CandidateAssessment(weight, value, value - base_value, worst, passes_guard)
            candidates.append(candidate)
            if (
                candidate.passes_guard
                and candidate.model_gain > min_model_gain
                and (selected is None or candidate.model_gain > selected.model_gain + 1e-12)
            ):
                selected = candidate
                selected_policy = policy
        budget.check_time()
        if selected is None:
            reason = (
                "worst_case_guard_rejected"
                if any(candidate.model_gain > min_model_gain for candidate in candidates)
                else "no_positive_model_gain"
            )
            return fallback(reason)
        return AdaptationResult(
            selected_policy,
            hero,
            True,
            "model_gain_and_worst_case_guard_passed",
            selected.weight,
            selected.model_gain,
            base_value,
            selected.model_value,
            base_worst,
            selected.worst_case_value,
            max_worst_case_loss,
            sample_count,
            min_samples,
            tuple(candidates),
            budget.nodes,
            time.monotonic() - started,
            "model_adaptation_exact_2p_guard",
            model_version,
        )
    except EvaluationLimitError:
        return fallback("budget_exhausted")
    except _CoverageError:
        return fallback("incomplete_policy_coverage")
    except (ValueError, KeyError, TypeError):
        return fallback("invalid_model_or_baseline")


accept = adapt_strategy
