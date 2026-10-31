"""Bounded public-belief search with private multi-valued continuations.

This is a small-game implementation of the *structure* in Pluribus: public
reach distribution, all private hands, a limited action horizon, and hidden
continuation choices by every active player. The four bias multipliers and
finite-game solver are explicit engineering choices, not recovered Pluribus
parameters. Re-solving here is conditional approximate search, without a safe
subgame-solving or multiplayer equilibrium guarantee.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from typing import Any

from texas_game_core.interfaces import CHANCE_PLAYER
from texas_strategy.cfr import Trainer, TrainingDiagnostics
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


def build_public_root(
    game: Any,
    public_key: Hashable,
    profile: Any,
    *,
    max_nodes: int = 250_000,
    max_worlds: int = 512,
    deadline_seconds: float | None = None,
    allow_uniform_fallback: bool = False,
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
    budget = TraversalBudget(max_nodes, deadline_seconds)
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
    total = math.fsum(worlds.values())
    if total <= 0:
        raise SearchCoverageError("public history is missing or has zero blueprint reach")
    fallbacks = _fallback_count(profile) - fallback_before
    return RootDistribution(
        public_key,
        tuple(worlds),
        tuple(reach / total for reach in worlds.values()),
        total,
        budget.nodes,
        "blueprint_public_reach_with_uniform_fallback" if fallbacks else "blueprint_public_reach",
        fallbacks,
    )


def action_semantics(action: str) -> str | None:
    """Explicitly map Kuhn check/bet and poker call/raise sizes to bias families."""
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


class DepthLimitedGame:
    """An extensive-game wrapper with one artificial public-belief chance root.

    ``depth_limit`` counts player actions, not chance events. At the cutoff,
    active players choose continuation policies in a fixed order. Later seats
    cannot observe earlier choices: the choices never enter their information
    keys. Leaf payoffs integrate the joint continuation profile for every seat.
    """

    def __init__(
        self,
        game: Any,
        root: RootDistribution,
        blueprint: Any,
        *,
        depth_limit: int = 2,
        continuation_multiplier: float = 4.0,
        max_rollout_nodes: int = 250_000,
        deadline_seconds: float | None = None,
        allow_uniform_fallback: bool = False,
    ) -> None:
        if type(depth_limit) is not int or depth_limit < 1:
            raise ValueError("depth_limit must be a positive player-action count")
        if not callable(getattr(game, "active_players", None)):
            raise SearchCoverageError(
                "game must expose active_players for leaf continuation choices"
            )
        ContinuationPolicy(blueprint, "blueprint", continuation_multiplier)
        self.game = game
        self.num_players = game.num_players
        self.game_id = f"{getattr(game, 'game_id', 'finite')}:search"
        self.rules_version = f"{getattr(game, 'rules_version', 'unknown')}:multivalue-v1"
        self.root_distribution = root
        self.blueprint = blueprint
        self.depth_limit = depth_limit
        self.continuation_multiplier = continuation_multiplier
        self.allow_uniform_fallback = allow_uniform_fallback
        self.rollout_budget = TraversalBudget(max_rollout_nodes, deadline_seconds)
        self._tail_cache: dict[tuple, tuple[float, ...]] = {}
        self._blueprint_observed: dict = {}
        self._root_labels = tuple(f"world:{i}" for i in range(len(root.states)))
        self._root_indices = {label: i for i, label in enumerate(self._root_labels)}
        root_actor = game.actor(root.states[0])
        if root_actor is None or root_actor == CHANCE_PLAYER:
            raise SearchCoverageError("public root must be a player decision")
        for state in root.states:
            if game.public_key(state) != root.public_key or game.actor(state) != root_actor:
                raise SearchCoverageError(
                    "root worlds must share the same public history and actor"
                )

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
        if self._at_cutoff(state):
            return next((p for p in self._active(state) if state.choices[p] is None), None)
        return self.game.actor(state.underlying)

    def legal_actions(self, state: SearchState) -> tuple[str, ...]:
        if state.root:
            return self._root_labels
        if self.actor(state) is None:
            return ()
        if self._at_cutoff(state):
            return CONTINUATION_ACTIONS
        return tuple(self.game.legal_actions(state.underlying))

    def chance_outcomes(self, state: SearchState) -> tuple[tuple[str, float], ...]:
        if state.root:
            return tuple(zip(self._root_labels, self.root_distribution.probabilities, strict=True))
        if self.actor(state) != CHANCE_PLAYER:
            return ()
        return checked_chance(self.game, state.underlying)

    def transition(self, state: SearchState, action: str) -> SearchState:
        if action not in self.legal_actions(state):
            raise ValueError("illegal subgame action")
        if state.root:
            return self.wrap_state(self.root_distribution.states[self._root_indices[action]])
        if self._at_cutoff(state):
            choices = list(state.choices)
            choices[self.actor(state)] = action.removeprefix("continue:")
            return SearchState(state.underlying, state.depth, tuple(choices))
        actor = self.actor(state)
        return SearchState(
            self.game.transition(state.underlying, action),
            state.depth + int(actor != CHANCE_PLAYER),
            state.choices,
        )

    def information_key(self, state: SearchState, player: int) -> str:
        if state.root:
            raise ValueError("artificial chance root has no player information set")
        if type(player) is not int or not 0 <= player < self.num_players:
            raise ValueError("invalid observing player")
        return json.dumps(
            [
                "multivalue-v1",
                "continuation" if self._at_cutoff(state) else "play",
                state.depth,
                self.game.information_key(state.underlying, player),
                state.choices[player],
            ],
            separators=(",", ":"),
        )

    def observation(self, state: SearchState, player: int) -> dict:
        """A debugging observation, with no other player's hidden continuation choice."""
        return {
            "information_key": self.information_key(state, player),
            "to_act": self.actor(state),
            "own_continuation": state.choices[player],
        }

    def terminal_payoffs(self, state: SearchState) -> tuple[float, ...]:
        if self.actor(state) is not None:
            raise ValueError("payoffs require a terminal subgame state")
        if self.game.actor(state.underlying) is None:
            return checked_payoffs(self.game, state.underlying)
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
        return self._tail_cache[key]


@dataclass(frozen=True)
class SearchResult:
    player: int
    information_key: str
    actions: tuple[str, ...]
    probabilities: tuple[float, ...]
    root_distribution: RootDistribution
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
    def root_worlds(self) -> int:
        return len(self.root_distribution.states)

    def to_dict(self) -> dict:
        return {
            "player": self.player,
            "information_key": self.information_key,
            "actions": list(self.actions),
            "probabilities": list(self.probabilities),
            "root_worlds": self.root_worlds,
            "root_reach_probability": self.root_distribution.reach_probability,
            "root_source": self.root_distribution.source,
            "public_root_nodes": self.root_distribution.nodes,
            "rollout_nodes": self.subgame.rollout_budget.nodes,
            "rollout_cache_entries": self.subgame.rollout_cache_entries,
            "depth_limit": self.subgame.depth_limit,
            "continuation_multiplier": self.subgame.continuation_multiplier,
            "continuations": list(CONTINUATIONS),
            "blueprint_fallback_queries": self.blueprint_fallback_queries,
            "diagnostics": self.diagnostics.to_dict(),
            "elapsed_seconds": self.elapsed_seconds,
            "quality": self.quality,
            "warnings": list(self.warnings)
            + (
                ["Untrained blueprint information sets used explicitly enabled uniform fallbacks."]
                if self.blueprint_fallback_queries
                else []
            )
            + (
                ["Caller-supplied public belief coverage and provenance have not been certified."]
                if self.root_distribution.source == "caller_supplied_public_belief"
                else []
            ),
        }


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
) -> SearchResult:
    """Solve for the entire public range; only then query the actual player's hand.

    ``target_state`` identifies the public history and the information set to
    return. Its private cards are never used to filter the public root. A caller
    supplied root is explicitly labeled and must itself be a public belief,
    including the hero's full range; the engine cannot certify its provenance.
    """
    started = time.monotonic()
    fallback_before = _fallback_count(blueprint)
    if type(iterations) is not int or iterations < 1:
        raise ValueError("search requires at least one iteration")
    if type(max_root_worlds) is not int or max_root_worlds < 1:
        raise ValueError("max_root_worlds must be a positive integer")
    clock = TraversalBudget(1, deadline_seconds)

    def remaining() -> float | None:
        clock.check_time()
        if deadline_seconds is None:
            return None
        seconds = deadline_seconds - (time.monotonic() - started)
        if seconds <= 0:
            raise EvaluationLimitError("search deadline exceeded")
        return seconds

    player = game.actor(target_state)
    if player is None or player == CHANCE_PLAYER:
        raise SearchCoverageError("target state must be a player decision")
    public_key = game.public_key(target_state)
    if root_distribution is None:
        root_distribution = build_public_root(
            game,
            public_key,
            blueprint,
            max_nodes=max_root_nodes,
            max_worlds=max_root_worlds,
            deadline_seconds=remaining(),
            allow_uniform_fallback=allow_uniform_fallback,
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
        deadline_seconds=remaining(),
        allow_uniform_fallback=allow_uniform_fallback,
    )
    requested_info = game.information_key(target_state, player)
    representative = next(
        (
            state
            for state in root_distribution.states
            if game.information_key(state, player) == requested_info
        ),
        None,
    )
    if representative is None:
        raise SearchCoverageError(
            "the requested private information set has zero public-root reach"
        )
    trainer = Trainer(subgame, algorithm=algorithm, weighting=weighting, seed=seed)
    diagnostics = trainer.train(iterations, max_nodes=max_nodes, deadline_seconds=remaining())
    if diagnostics.completed_iterations == 0:
        raise EvaluationLimitError("search budget allowed no complete training iteration")
    policy = trainer.average_policy(missing="error")
    decision_state = subgame.wrap_state(representative)
    if not policy.contains(subgame, decision_state):
        raise SearchCoverageError("search did not train the requested private information set")
    probabilities = checked_probabilities(subgame, decision_state, policy)
    return SearchResult(
        player,
        requested_info,
        tuple(game.legal_actions(representative)),
        probabilities,
        root_distribution,
        policy,
        subgame,
        diagnostics,
        time.monotonic() - started,
        _fallback_count(blueprint) - fallback_before,
    )


search = solve_subgame
