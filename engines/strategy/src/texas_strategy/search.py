"""Bounded public-belief search with private multi-valued continuations.

This implements the *structure* in Pluribus: public
reach distribution, all private hands, a limited action horizon, and hidden
continuation choices by every active player. The four bias multipliers and
finite-game solver are explicit engineering choices, not recovered Pluribus
parameters. Re-solving here is conditional approximate search, without a safe
subgame-solving or multiplayer equilibrium guarantee. Sampled continuations
expose the fixed joint policy as chance nodes, so MCCFR samples complete paths
without enumerating future cards or caching one noisy draw as a fixed leaf value.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from random import Random
from typing import Any

from texas_game_core.interfaces import CHANCE_PLAYER
from texas_strategy.cfr import Trainer, TrainingDiagnostics
from texas_strategy.evaluation import CancellationError as CancellationError
from texas_strategy.evaluation import (
    EvaluationLimitError,
    TraversalBudget,
    checked_chance,
    checked_payoffs,
    checked_probabilities,
    evaluate_policy,
    policy_for_player,
)

CONTINUATIONS = ("blueprint", "fold", "call", "raise")
CONTINUATION_ACTIONS = tuple(f"continue:{name}" for name in CONTINUATIONS)


class SearchCoverageError(ValueError):
    """A public root or the requested hand has no supported search coverage."""


def _search_information_key(game, state, player):
    return getattr(game, "search_information_key", game.information_key)(state, player)


def _fallback_count(profile: Any) -> int:
    policies = profile if isinstance(profile, Sequence) else (profile,)
    unique = {id(policy): policy for policy in policies}
    return sum(getattr(policy, "fallback_count", 0) for policy in unique.values())


def _blueprint_probabilities(
    game: Any, state: Any, profile: Any, observed: dict | None, allow_uniform_fallback: bool
) -> tuple[float, ...]:
    policy = policy_for_player(profile, game.num_players, game.actor(state))
    if (
        not allow_uniform_fallback
        and callable(getattr(policy, "contains", None))
        and not policy.contains(game, state)
    ):
        raise SearchCoverageError("blueprint does not cover an information set needed by search")
    return checked_probabilities(game, state, profile, observed)


@dataclass(frozen=True)
class RootDistribution:
    public_key: Hashable
    states: tuple[Any, ...]
    probabilities: tuple[float, ...]
    reach_probability: float
    nodes: int = 0
    source: str = "blueprint_public_reach"
    blueprint_fallback_queries: int = 0

    def __post_init__(self) -> None:
        if not self.states or len(self.states) != len(self.probabilities):
            raise ValueError("public root requires an aligned, nonempty state distribution")
        if any(not math.isfinite(p) or p <= 0 for p in self.probabilities):
            raise ValueError("public root probabilities must be positive and finite")
        if not math.isclose(math.fsum(self.probabilities), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError("public root probabilities must sum to one")
        if not math.isfinite(self.reach_probability) or not 0 < self.reach_probability <= 1 + 1e-9:
            raise ValueError("public root requires a positive reach probability of at most one")
        if len(set(self.states)) != len(self.states):
            raise ValueError("public root states must be immutable, distinct hidden worlds")


@dataclass(frozen=True)
class SampledRoot:
    """Fixed public belief with a known proposal and unnormalized target weight.

    ``sample`` must include every positive-target private world in its support,
    without conditioning on the requested player's actual cards. The caller
    owns the fixed rules, evidence and policy snapshot used by this callback.
    A validated zero target weight may return None instead of a child state.
    """

    public_key: Hashable
    actor: int
    sample: Callable[[Random], tuple[Any, float, float]]

    def __post_init__(self) -> None:
        hash(self.public_key)
        if type(self.actor) is not int or self.actor < 0 or not callable(self.sample):
            raise ValueError("sampled root requires a player actor and callable proposal")


def build_public_root(
    game: Any,
    public_key: Hashable,
    profile: Any,
    *,
    max_nodes: int = 250_000,
    max_worlds: int = 512,
    deadline_seconds: float | None = None,
    allow_uniform_fallback: bool = False,
    should_cancel: Callable[[], bool] | None = None,
) -> RootDistribution:
    """Normalize blueprint reach over *all* worlds at a public decision history.

    There is deliberately no actual-hand argument. Both hero and opponent
    private hands remain in the public belief. Zero reach is unsupported rather
    than replaced with an invented uniform range. Large games must supply a
    bounded public belief explicitly; no chance distribution is truncated.
    """
    if not callable(getattr(game, "public_key", None)):
        raise SearchCoverageError("game must expose a private-information-free public_key")
    if getattr(game, "supports_exact_enumeration", True) is False:
        raise SearchCoverageError("this game requires an explicitly supplied bounded public root")
    if type(max_worlds) is not int or max_worlds < 1:
        raise ValueError("max_worlds must be a positive integer")
    budget = TraversalBudget(max_nodes, deadline_seconds, should_cancel)
    observed: dict = {}
    worlds: dict[Any, float] = {}
    fallback_before = _fallback_count(profile)

    def visit(state: Any, reach: float) -> None:
        budget.tick()
        actor = game.actor(state)
        if game.public_key(state) == public_key:
            if actor is None or actor == CHANCE_PLAYER:
                raise SearchCoverageError("the public root must be a player decision")
            worlds[state] = worlds.get(state, 0.0) + reach
            if len(worlds) > max_worlds:
                raise EvaluationLimitError("public root exceeds hidden-world budget")
            return
        if actor is None:
            return
        if actor == CHANCE_PLAYER:
            outcomes = checked_chance(game, state)
        else:
            outcomes = tuple(
                zip(
                    game.legal_actions(state),
                    _blueprint_probabilities(
                        game, state, profile, observed, allow_uniform_fallback
                    ),
                    strict=True,
                )
            )
        for action, probability in outcomes:
            if probability > 0:
                next_reach = reach * probability
                if next_reach == 0:
                    raise EvaluationLimitError("public reach underflow; no complete root available")
                visit(game.transition(state, action), next_reach)

    visit(game.initial_state(), 1.0)
    budget.check_time()
    total = math.fsum(worlds.values())
    if total <= 0:
        raise SearchCoverageError("public history is missing or has zero blueprint reach")
    fallbacks = _fallback_count(profile) - fallback_before
    root = RootDistribution(
        public_key,
        tuple(worlds),
        tuple(reach / total for reach in worlds.values()),
        total,
        budget.nodes,
        "blueprint_public_reach_with_uniform_fallback" if fallbacks else "blueprint_public_reach",
        fallbacks,
    )
    budget.check_time()
    return root


def action_semantics(action: str) -> str | None:
    """Explicitly map Kuhn check/bet and poker call/raise sizes to bias families."""
    if action == "all_in":
        return "raise"
    family = action.lower().split(":", 1)[0].split("_", 1)[0]
    if family == "fold":
        return "fold"
    if family in ("call", "check"):
        return "call"
    if family in ("bet", "raise"):
        return "raise"
    return None


@dataclass(frozen=True)
class ContinuationPolicy:
    blueprint: Any
    mode: str
    multiplier: float = 4.0
    observed: dict | None = None
    allow_uniform_fallback: bool = False

    def __post_init__(self) -> None:
        if self.mode not in CONTINUATIONS:
            raise ValueError("unknown continuation policy")
        if not math.isfinite(self.multiplier) or self.multiplier < 1:
            raise ValueError("continuation multiplier must be finite and at least one")

    def probabilities(self, game: Any, state: Any) -> tuple[float, ...]:
        baseline = _blueprint_probabilities(
            game, state, self.blueprint, self.observed, self.allow_uniform_fallback
        )
        weights = tuple(
            probability * (self.multiplier if action_semantics(action) == self.mode else 1.0)
            for action, probability in zip(game.legal_actions(state), baseline, strict=True)
        )
        total = math.fsum(weights)
        return tuple(weight / total for weight in weights)


@dataclass(frozen=True, slots=True)
class SearchState:
    underlying: Any = None
    depth: int = 0
    choices: tuple[str | None, ...] = ()
    root: bool = False
    rollout: bool = False


class DepthLimitedGame:
    """An extensive-game wrapper with one artificial public-belief chance root.

    ``depth_limit`` counts player actions, not chance events. At the cutoff,
    active players choose continuation policies in a fixed order. Later seats
    cannot observe earlier choices: the choices never enter their information
    keys. Exact leaf payoffs integrate the joint continuation profile. With
    sampled_rollouts, those same policies become chance transitions until the
    underlying game ends; the trainer owns the RNG and samples anew each visit.
    """

    def __init__(
        self,
        game: Any,
        root: RootDistribution | SampledRoot,
        blueprint: Any,
        *,
        depth_limit: int = 2,
        continuation_multiplier: float = 4.0,
        max_rollout_nodes: int = 250_000,
        deadline_seconds: float | None = None,
        allow_uniform_fallback: bool = False,
        sampled_rollouts: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        if type(depth_limit) is not int or depth_limit < 1:
            raise ValueError("depth_limit must be a positive player-action count")
        if type(sampled_rollouts) is not bool:
            raise ValueError("sampled_rollouts must be boolean")
        if not callable(getattr(game, "active_players", None)):
            raise SearchCoverageError(
                "game must expose active_players for leaf continuation choices"
            )
        ContinuationPolicy(blueprint, "blueprint", continuation_multiplier)
        self.game = game
        self.num_players = game.num_players
        self.game_id = f"{getattr(game, 'game_id', 'finite')}:search"
        self.rules_version = f"{getattr(game, 'rules_version', 'unknown')}:multivalue-v1"
        if sampled_rollouts:
            self.rules_version += ":sampled-trajectories-v1"
        self.root_distribution = root
        self.blueprint = blueprint
        self.depth_limit = depth_limit
        self.continuation_multiplier = continuation_multiplier
        self.allow_uniform_fallback = allow_uniform_fallback
        self.sampled_rollouts = sampled_rollouts
        self.rollout_budget = TraversalBudget(max_rollout_nodes, deadline_seconds, should_cancel)
        self.rollout_budget.check_time()
        self._tail_cache: dict[tuple, tuple[float, ...]] = {}
        self.rollout_samples = 0
        # Share a fixed policy lookup between sampling and validation, retaining only one state.
        self._rollout_chance: tuple[SearchState | None, tuple[tuple[str, float], ...]] = (None, ())
        self._blueprint_observed: dict = {}
        self.root_draws = 0
        self.root_positive_draws = 0
        self._root_weight_scale = 0.0
        self._root_weight_sum = 0.0
        self._root_weight_square_sum = 0.0
        if isinstance(root, SampledRoot):
            if not sampled_rollouts:
                raise ValueError("generated roots require sampled rollouts")
            if root.actor >= self.num_players:
                raise SearchCoverageError("sampled root actor is outside the player roster")
            self.rules_version += ":generated-public-root-v1"
            self.supports_exact_enumeration = False
            # Finite roots must not expose this hook: their chance protocol is unchanged.
            self.sample_training_root = self._sample_training_root
            return
        self._root_labels = tuple(f"world:{i}" for i in range(len(root.states)))
        self._root_indices = {label: i for i, label in enumerate(self._root_labels)}
        root_actor = game.actor(root.states[0])
        if root_actor is None or root_actor == CHANCE_PLAYER:
            raise SearchCoverageError("public root must be a player decision")
        for state in root.states:
            self.rollout_budget.check_time()
            if game.public_key(state) != root.public_key or game.actor(state) != root_actor:
                raise SearchCoverageError(
                    "root worlds must share the same public history and actor"
                )
        self.rollout_budget.check_time()

    def _sample_training_root(self, rng: Random) -> tuple[SearchState | None, float, float]:
        self.rollout_budget.check_time()
        outcome = self.root_distribution.sample(rng)
        self.rollout_budget.check_time()
        if not isinstance(outcome, tuple) or len(outcome) != 3:
            raise ValueError("sampled root must return (child, q, w)")
        underlying, q, w = outcome
        try:
            valid = (
                type(q) in (int, float)
                and math.isfinite(q)
                and 0 < q <= 1
                and type(w) in (int, float)
                and math.isfinite(w)
                and w >= 0
                and math.isfinite(w / q)
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("sampled root requires finite q, w and w/q with 0 < q <= 1 and w >= 0")
        if w > 0:
            actor = self.game.actor(underlying)
            if (
                self.game.public_key(underlying) != self.root_distribution.public_key
                or type(actor) is not int
                or actor != self.root_distribution.actor
            ):
                raise SearchCoverageError(
                    "sampled world must match the fixed public history and actor"
                )
        result = (self.wrap_state(underlying) if w > 0 else None), q, w
        self.rollout_budget.check_time()
        self.root_draws += 1
        if w > 0:
            self.root_positive_draws += 1
            weight = w / q
            # Rescale diagnostics only; never rescale or clip the trainer's weights.
            if weight > self._root_weight_scale:
                scale = self._root_weight_scale / weight
                self._root_weight_sum *= scale
                self._root_weight_square_sum *= scale * scale
                self._root_weight_scale = weight
            scaled = weight / self._root_weight_scale
            self._root_weight_sum += scaled
            self._root_weight_square_sum += scaled * scaled
        return result

    @property
    def root_effective_sample_size(self) -> float:
        if not self._root_weight_square_sum:
            return 0.0
        return self._root_weight_sum**2 / self._root_weight_square_sum

    @property
    def rollout_cache_entries(self) -> int:
        return len(self._tail_cache)

    def initial_state(self) -> SearchState:
        return SearchState(root=True)

    def wrap_state(self, state: Any) -> SearchState:
        return SearchState(state, 0, (None,) * self.num_players)

    def _at_cutoff(self, state: SearchState) -> bool:
        return (
            not state.root
            and not state.rollout
            and state.depth >= self.depth_limit
            and self.game.actor(state.underlying) is not None
        )

    def _active(self, state: SearchState) -> tuple[int, ...]:
        active = tuple(self.game.active_players(state.underlying))
        if not active or len(set(active)) != len(active):
            raise ValueError("active players must be nonempty and unique at a search cutoff")
        if any(type(p) is not int or not 0 <= p < self.num_players for p in active):
            raise ValueError("invalid active player at search cutoff")
        return active

    def actor(self, state: SearchState) -> int | None:
        if state.root:
            return CHANCE_PLAYER
        if state.rollout:
            return None if self.game.actor(state.underlying) is None else CHANCE_PLAYER
        if self._at_cutoff(state):
            return next((p for p in self._active(state) if state.choices[p] is None), None)
        return self.game.actor(state.underlying)

    def legal_actions(self, state: SearchState) -> tuple[str, ...]:
        if state.root:
            if isinstance(self.root_distribution, SampledRoot):
                raise ValueError("generated root requires the initial-root proposal sampler")
            return self._root_labels
        if self.actor(state) is None:
            return ()
        if self._at_cutoff(state):
            return CONTINUATION_ACTIONS
        return tuple(self.game.legal_actions(state.underlying))

    def chance_outcomes(self, state: SearchState) -> tuple[tuple[str, float], ...]:
        self.rollout_budget.check_time()
        if state.root:
            if isinstance(self.root_distribution, SampledRoot):
                raise ValueError("generated root requires the initial-root proposal sampler")
            return tuple(zip(self._root_labels, self.root_distribution.probabilities, strict=True))
        if self.actor(state) != CHANCE_PLAYER:
            return ()
        if state.rollout and self.game.actor(state.underlying) != CHANCE_PLAYER:
            if self._rollout_chance[0] is state:
                return self._rollout_chance[1]
            outcomes = tuple(
                (action, probability)
                for action, probability in zip(
                    self.game.legal_actions(state.underlying),
                    self._continuation_probabilities(state),
                    strict=True,
                )
                if probability > 0
            )
            self._rollout_chance = state, outcomes
            self.rollout_budget.check_time()
            return outcomes
        outcomes = checked_chance(self.game, state.underlying)
        self.rollout_budget.check_time()
        return outcomes

    def _continuation_probabilities(self, state: SearchState) -> tuple[float, ...]:
        actor = self.game.actor(state.underlying)
        return ContinuationPolicy(
            self.blueprint,
            state.choices[actor] or "blueprint",
            self.continuation_multiplier,
            self._blueprint_observed,
            self.allow_uniform_fallback,
        ).probabilities(self.game, state.underlying)

    def sample_chance_outcome(self, state: SearchState, rng) -> tuple[str, float]:
        if self.actor(state) != CHANCE_PLAYER:
            raise ValueError("state is not a chance node")
        self.rollout_budget.check_time()
        if not state.root and self.game.actor(state.underlying) == CHANCE_PLAYER:
            sampler = getattr(self.game, "sample_chance_outcome", None)
            if callable(sampler):
                outcome = sampler(state.underlying, rng)
                self.rollout_budget.check_time()
                return outcome
        outcomes = self.chance_outcomes(state)
        threshold, cumulative = rng.random(), 0.0
        positive = tuple(
            (action, probability) for action, probability in outcomes if probability > 0
        )
        for action, probability in positive:
            cumulative += probability
            if threshold < cumulative:
                return action, probability
        return positive[-1]

    def transition(self, state: SearchState, action: str) -> SearchState:
        self.rollout_budget.check_time()
        if state.root:
            if isinstance(self.root_distribution, SampledRoot):
                raise ValueError("generated root requires the initial-root proposal sampler")
            if action not in self._root_indices:
                raise ValueError("illegal subgame action")
            result = self.wrap_state(self.root_distribution.states[self._root_indices[action]])
            self.rollout_budget.check_time()
            return result
        if self._at_cutoff(state):
            if self.actor(state) is None or action not in CONTINUATION_ACTIONS:
                raise ValueError("illegal subgame action")
            choices = list(state.choices)
            choices[self.actor(state)] = action.removeprefix("continue:")
            rollout = self.sampled_rollouts and all(
                choices[p] is not None for p in self._active(state)
            )
            result = SearchState(state.underlying, state.depth, tuple(choices), rollout=rollout)
            self.rollout_budget.check_time()
            return result
        actor = self.actor(state)
        if actor is None:
            raise ValueError("illegal subgame action")
        if state.rollout:
            self.rollout_budget.tick()
            if self.game.actor(state.underlying) != CHANCE_PLAYER:
                outcomes = self.chance_outcomes(state)
                if not any(a == action and p > 0 for a, p in outcomes):
                    raise ValueError("action has zero continuation probability")
            # The underlying transition validates card draws without enumerating them.
            underlying = self.game.transition(state.underlying, action)
            terminal = self.game.actor(underlying) is None
            result = SearchState(
                underlying,
                state.depth,
                state.choices,
                rollout=True,
            )
            self.rollout_budget.check_time()
            self.rollout_samples += int(terminal)
            return result
        if actor != CHANCE_PLAYER and action not in self.legal_actions(state):
            raise ValueError("illegal subgame action")
        result = SearchState(
            self.game.transition(state.underlying, action),
            state.depth + int(actor != CHANCE_PLAYER),
            state.choices,
        )
        self.rollout_budget.check_time()
        return result

    def information_key(self, state: SearchState, player: int) -> str:
        if state.root or state.rollout:
            raise ValueError("chance-only search state has no player information set")
        if type(player) is not int or not 0 <= player < self.num_players:
            raise ValueError("invalid observing player")
        if state.depth == 0:
            return self.root_information_key(
                _search_information_key(self.game, state.underlying, player)
            )
        return json.dumps(
            [
                "multivalue-v1",
                "continuation" if self._at_cutoff(state) else "play",
                state.depth,
                _search_information_key(self.game, state.underlying, player),
                state.choices[player],
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def root_information_key(information_key: str) -> str:
        return json.dumps(
            ["multivalue-v1", "play", 0, information_key, None], separators=(",", ":")
        )

    def observation(self, state: SearchState, player: int) -> dict:
        """A debugging observation, with no other player's hidden continuation choice."""
        return {
            "information_key": self.information_key(state, player),
            "to_act": self.actor(state),
            "own_continuation": state.choices[player],
        }

    def terminal_payoffs(self, state: SearchState) -> tuple[float, ...]:
        self.rollout_budget.check_time()
        if self.actor(state) is not None:
            raise ValueError("payoffs require a terminal subgame state")
        if self.game.actor(state.underlying) is None:
            values = checked_payoffs(self.game, state.underlying)
            self.rollout_budget.check_time()
            return values
        key = (state.underlying, state.choices)
        if key not in self._tail_cache:
            profile = tuple(
                ContinuationPolicy(
                    self.blueprint,
                    mode or "blueprint",
                    self.continuation_multiplier,
                    self._blueprint_observed,
                    self.allow_uniform_fallback,
                )
                for mode in state.choices
            )
            self._tail_cache[key] = evaluate_policy(
                self.game, profile, start_state=state.underlying, budget=self.rollout_budget
            )
        self.rollout_budget.check_time()
        return self._tail_cache[key]


@dataclass(frozen=True)
class SearchResult:
    player: int
    information_key: str
    actions: tuple[str, ...]
    probabilities: tuple[float, ...]
    root_distribution: RootDistribution | SampledRoot
    policy: Any
    subgame: DepthLimitedGame
    diagnostics: TrainingDiagnostics
    elapsed_seconds: float
    blueprint_fallback_queries: int = 0
    quality: str = "conditional_approximate_search"
    warnings: tuple[str, ...] = (
        "Finite continuation set and local re-solving do not guarantee a safe global equilibrium.",
        "Continuation biases preserve blueprint support and cannot revive absent actions.",
    )

    @property
    def root_worlds(self) -> int | None:
        if isinstance(self.root_distribution, SampledRoot):
            return None
        return len(self.root_distribution.states)

    def to_dict(self) -> dict:
        generated = isinstance(self.root_distribution, SampledRoot)
        result = {
            "player": self.player,
            "information_key": self.information_key,
            "actions": list(self.actions),
            "probabilities": list(self.probabilities),
            "root_worlds": self.root_worlds,
            "root_reach_probability": None
            if generated
            else self.root_distribution.reach_probability,
            "root_source": "generated_public_belief"
            if generated
            else self.root_distribution.source,
            "public_root_nodes": self.subgame.root_draws
            if generated
            else self.root_distribution.nodes,
            "rollout_nodes": self.subgame.rollout_budget.nodes,
            "rollout_cache_entries": self.subgame.rollout_cache_entries,
            "rollout_samples": self.subgame.rollout_samples,
            "continuation_evaluation": "sampled_trajectories"
            if self.subgame.sampled_rollouts
            else "exact_expectation",
            "depth_limit": self.subgame.depth_limit,
            "continuation_multiplier": self.subgame.continuation_multiplier,
            "continuations": list(CONTINUATIONS),
            "blueprint_fallback_queries": self.blueprint_fallback_queries,
            "diagnostics": self.diagnostics.to_dict(),
            "elapsed_seconds": self.elapsed_seconds,
            "quality": self.quality,
            "warnings": list(self.warnings)
            + (
                ["Continuation values are sampled, with no convergence or error-bound certificate."]
                if self.subgame.sampled_rollouts
                else []
            )
            + (
                ["Untrained blueprint information sets used explicitly enabled uniform fallbacks."]
                if self.blueprint_fallback_queries
                else []
            )
            + (
                ["Caller-supplied public belief coverage and provenance have not been certified."]
                if generated or self.root_distribution.source == "caller_supplied_public_belief"
                else []
            ),
        }
        if generated:
            result.update(
                root_draws=self.subgame.root_draws,
                root_positive_draws=self.subgame.root_positive_draws,
                root_effective_sample_size=self.subgame.root_effective_sample_size,
                root_sampling_scope="all_draws_including_discarded_work",
                root_coverage="sampled_target_only_full_range_not_certified",
            )
            result["warnings"].append(
                "Generated roots do not guarantee finite-budget private-hand coverage; "
                "normalized reach and posterior EV are unknown. Root ESS is diagnostic only."
            )
        return result


def solve_subgame(
    game: Any,
    target_state: Any,
    blueprint: Any,
    *,
    depth_limit: int = 2,
    iterations: int = 100,
    algorithm: str = "cfr",
    weighting: str = "linear",
    seed: int = 0,
    continuation_multiplier: float = 4.0,
    root_distribution: RootDistribution | None = None,
    max_root_nodes: int = 250_000,
    max_root_worlds: int = 512,
    max_rollout_nodes: int = 250_000,
    max_nodes: int = 1_000_000,
    deadline_seconds: float | None = None,
    allow_uniform_fallback: bool = False,
    sampled_rollouts: bool = False,
    should_cancel: Callable[[], bool] | None = None,
) -> SearchResult:
    """Solve for the entire public range; only then query the actual player's hand.

    ``target_state`` identifies the public history and the information set to
    return. Its private cards are never used to filter the public root. A caller
    supplied root is explicitly labeled and must itself be a public belief,
    including the hero's full range; the engine cannot certify its provenance.
    Cancellation raises CancellationError, independently of budget exhaustion.
    Root construction, training, tails and final policy conversion share one
    wall deadline; neither cancellation nor expiry returns an earlier policy.
    """
    started = time.monotonic()
    fallback_before = _fallback_count(blueprint)
    if type(iterations) is not int or iterations < 1:
        raise ValueError("search requires at least one iteration")
    if sampled_rollouts and algorithm != "mccfr":
        raise ValueError("sampled rollouts require algorithm=mccfr")
    if type(max_root_worlds) is not int or max_root_worlds < 1:
        raise ValueError("max_root_worlds must be a positive integer")
    clock = TraversalBudget(1, deadline_seconds, should_cancel)
    clock.started = started

    def check_search() -> bool:
        clock.check_time()
        return False

    check_search()
    player = game.actor(target_state)
    check_search()
    if player is None or player == CHANCE_PLAYER:
        raise SearchCoverageError("target state must be a player decision")
    public_key = game.public_key(target_state)
    check_search()
    if root_distribution is None:
        root_distribution = build_public_root(
            game,
            public_key,
            blueprint,
            max_nodes=max_root_nodes,
            max_worlds=max_root_worlds,
            allow_uniform_fallback=allow_uniform_fallback,
            should_cancel=check_search,
        )
    else:
        if root_distribution.public_key != public_key:
            raise SearchCoverageError("supplied root does not match the target public history")
        # Mark caller provenance even if it claimed to be an enumerated root.
        root_distribution = RootDistribution(
            root_distribution.public_key,
            root_distribution.states,
            root_distribution.probabilities,
            root_distribution.reach_probability,
            root_distribution.nodes,
            "caller_supplied_public_belief",
            root_distribution.blueprint_fallback_queries,
        )
    if len(root_distribution.states) > max_root_worlds:
        raise EvaluationLimitError("public root exceeds hidden-world budget")
    subgame = DepthLimitedGame(
        game,
        root_distribution,
        blueprint,
        depth_limit=depth_limit,
        continuation_multiplier=continuation_multiplier,
        max_rollout_nodes=max_rollout_nodes,
        allow_uniform_fallback=allow_uniform_fallback,
        sampled_rollouts=sampled_rollouts,
        should_cancel=check_search,
    )
    requested_info = _search_information_key(game, target_state, player)
    representative = None
    for state in root_distribution.states:
        check_search()
        if _search_information_key(game, state, player) == requested_info:
            representative = state
            break
    check_search()
    if representative is None:
        raise SearchCoverageError(
            "the requested private information set has zero public-root reach"
        )
    return _train_search(
        subgame,
        player=player,
        requested_info=requested_info,
        representative=representative,
        iterations=iterations,
        algorithm=algorithm,
        weighting=weighting,
        seed=seed,
        max_nodes=max_nodes,
        clock=clock,
        fallback_before=fallback_before,
    )


def solve_generated_subgame(
    game: Any,
    root: SampledRoot,
    blueprint: Any,
    *,
    target_player: int,
    target_information_key: str,
    depth_limit: int = 2,
    iterations: int = 100,
    weighting: str = "linear",
    seed: int = 0,
    continuation_multiplier: float = 4.0,
    max_rollout_nodes: int = 250_000,
    max_nodes: int = 1_000_000,
    deadline_seconds: float | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SearchResult:
    """Search a fixed generative public belief, then query one private information set.

    No hidden target state or finite world list is required. The target key is
    the underlying game's search_information_key (information_key if absent).
    Only MCCFR with sampled continuations is supported; uncovered policies and
    a target absent from the completed iterations fail explicitly. The caller
    keeps the root evidence and blueprint snapshot fixed until this returns.
    """
    started = time.monotonic()
    fallback_before = _fallback_count(blueprint)
    if not isinstance(root, SampledRoot):
        raise ValueError("generated search requires a SampledRoot")
    if type(iterations) is not int or iterations < 1:
        raise ValueError("search requires at least one iteration")
    if type(target_player) is not int or target_player != root.actor:
        raise SearchCoverageError("target player must be the public root actor")
    if not isinstance(target_information_key, str) or not target_information_key:
        raise ValueError("target information key must be a nonempty string")
    clock = TraversalBudget(1, deadline_seconds, should_cancel)
    clock.started = started

    def check_search() -> bool:
        clock.check_time()
        return False

    check_search()
    subgame = DepthLimitedGame(
        game,
        root,
        blueprint,
        depth_limit=depth_limit,
        continuation_multiplier=continuation_multiplier,
        max_rollout_nodes=max_rollout_nodes,
        sampled_rollouts=True,
        should_cancel=check_search,
    )
    return _train_search(
        subgame,
        player=target_player,
        requested_info=target_information_key,
        iterations=iterations,
        algorithm="mccfr",
        weighting=weighting,
        seed=seed,
        max_nodes=max_nodes,
        clock=clock,
        fallback_before=fallback_before,
    )


def _train_search(
    subgame: DepthLimitedGame,
    *,
    player: int,
    requested_info: str,
    iterations: int,
    algorithm: str,
    weighting: str,
    seed: int,
    max_nodes: int,
    clock: TraversalBudget,
    fallback_before: int,
    representative: Any = None,
) -> SearchResult:
    def check_search() -> bool:
        clock.check_time()
        return False

    trainer = Trainer(subgame, algorithm=algorithm, weighting=weighting, seed=seed)
    training_seconds = None
    if clock.deadline_seconds is not None:
        remaining = clock.deadline_seconds - (time.monotonic() - clock.started)
        # Reserve time to publish completed iterations; the unfinished iteration rolls back.
        training_seconds = max(0.0, remaining - min(0.05, remaining / 10))
    diagnostics = trainer.train(
        iterations,
        max_nodes=max_nodes,
        deadline_seconds=training_seconds,
        should_cancel=check_search,
    )
    check_search()
    if diagnostics.completed_iterations == 0:
        raise EvaluationLimitError("search budget allowed no complete training iteration")
    policy = trainer.average_policy(missing="error", check=clock.check_time)
    entry = policy.lookup(player, subgame.root_information_key(requested_info))
    if entry is None:
        raise SearchCoverageError("search did not train the requested private information set")
    if representative is None:
        actions, probabilities = entry.actions, entry.probabilities
    else:
        decision_state = subgame.wrap_state(representative)
        probabilities = checked_probabilities(subgame, decision_state, policy)
        actions = tuple(subgame.game.legal_actions(representative))
    result = SearchResult(
        player,
        requested_info,
        actions,
        probabilities,
        subgame.root_distribution,
        policy,
        subgame,
        diagnostics,
        time.monotonic() - clock.started,
        _fallback_count(subgame.blueprint) - fallback_before,
    )
    check_search()
    return result


search = solve_subgame
