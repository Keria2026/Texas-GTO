"""Independent, bounded evaluation of finite imperfect-information games.

Best responses maximize once per information set, using opponents/chance reach.
They never maximize separately for each hidden world. Exact enumeration is for
small games; exceeding its budget is an error, never a truncated estimate.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field
from statistics import stdev
from typing import Any

from texas_game_core.interfaces import CHANCE_PLAYER


class EvaluationLimitError(RuntimeError):
    """The requested complete calculation did not fit the declared budget."""


@dataclass(slots=True)
class TraversalBudget:
    max_nodes: int = 250_000
    deadline_seconds: float | None = None
    nodes: int = field(init=False, default=0)
    started: float = field(init=False, default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if isinstance(self.max_nodes, bool) or self.max_nodes < 1:
            raise ValueError("max_nodes must be a positive integer")
        if not isinstance(self.max_nodes, int):
            raise ValueError("max_nodes must be a positive integer")
        if self.deadline_seconds is not None and (
            not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0
        ):
            raise ValueError("deadline_seconds must be positive and finite")

    def check_time(self) -> None:
        if (
            self.deadline_seconds is not None
            and time.monotonic() - self.started >= self.deadline_seconds
        ):
            raise EvaluationLimitError("evaluation deadline exceeded; no exact result available")

    def tick(self) -> None:
        self.check_time()
        if self.nodes >= self.max_nodes:
            raise EvaluationLimitError("evaluation node budget exceeded; no exact result available")
        self.nodes += 1


def policy_for_player(profile: Any, num_players: int, player: int) -> Any:
    """Accept one joint policy object or one policy object for each seat."""
    if isinstance(profile, Sequence):
        if len(profile) != num_players:
            raise ValueError("strategy profile must contain one policy per player")
        policy = profile[player]
    else:
        policy = profile
    if not callable(getattr(policy, "probabilities", None)):
        raise TypeError("a policy must implement probabilities(game, state)")
    return policy


def checked_probabilities(
    game: Any,
    state: Any,
    profile: Any,
    observed: dict | None = None,
) -> tuple[float, ...]:
    """Check legal support and consistency across all worlds of an information set."""
    player = game.actor(state)
    if player is None or player == CHANCE_PLAYER or not 0 <= player < game.num_players:
        raise ValueError("policy probabilities require a player decision")
    actions = tuple(game.legal_actions(state))
    if not actions or len(set(actions)) != len(actions):
        raise ValueError("legal player actions must be nonempty and unique")
    policy = policy_for_player(profile, game.num_players, player)
    probabilities = tuple(float(p) for p in policy.probabilities(game, state))
    if len(probabilities) != len(actions):
        raise ValueError("policy probability count differs from legal action count")
    if any(not math.isfinite(p) or p < 0 for p in probabilities):
        raise ValueError("policy probabilities must be finite and nonnegative")
    if not math.isclose(math.fsum(probabilities), 1.0, rel_tol=0, abs_tol=1e-9):
        raise ValueError("policy probabilities must sum to one")
    if observed is not None:
        key = (player, game.information_key(state, player))
        previous = observed.setdefault(key, (actions, probabilities))
        if previous[0] != actions or any(
            abs(a - b) > 1e-12 for a, b in zip(previous[1], probabilities, strict=True)
        ):
            raise ValueError("policy or legal actions depend on hidden information")
    return probabilities


def checked_chance(game: Any, state: Any) -> tuple[tuple[Hashable, float], ...]:
    outcomes = tuple((action, float(p)) for action, p in game.chance_outcomes(state))
    if not outcomes or len({action for action, _ in outcomes}) != len(outcomes):
        raise ValueError("chance outcomes must be nonempty and unique")
    if any(not math.isfinite(p) or p < 0 for _, p in outcomes):
        raise ValueError("chance probabilities must be finite and nonnegative")
    if not math.isclose(math.fsum(p for _, p in outcomes), 1.0, rel_tol=0, abs_tol=1e-9):
        raise ValueError("chance probabilities must sum to one")
    return outcomes


def checked_payoffs(game: Any, state: Any) -> tuple[float, ...]:
    values = tuple(float(value) for value in game.terminal_payoffs(state))
    if len(values) != game.num_players or any(not math.isfinite(v) for v in values):
        raise ValueError("terminal payoff must be a finite value vector for all players")
    return values


def evaluate_policy(
    game: Any,
    profile: Any,
    *,
    start_state: Any = None,
    max_nodes: int = 250_000,
    deadline_seconds: float | None = None,
    budget: TraversalBudget | None = None,
) -> tuple[float, ...]:
    """Return exact on-policy values, optionally from a specified underlying state.

    ``start_state`` is an internal rollout primitive: it is not a public belief
    distribution and must not be used as the root of an imperfect-information
    search conditional on the user's actual private cards.
    """
    if start_state is None and getattr(game, "supports_exact_enumeration", True) is False:
        raise EvaluationLimitError("this game does not support full-tree exact enumeration")
    budget = budget or TraversalBudget(max_nodes, deadline_seconds)
    observed: dict = {}

    def visit(state: Any) -> tuple[float, ...]:
        budget.tick()
        actor = game.actor(state)
        if actor is None:
            return checked_payoffs(game, state)
        if actor == CHANCE_PLAYER:
            outcomes = checked_chance(game, state)
        else:
            probabilities = checked_probabilities(game, state, profile, observed)
            outcomes = tuple(zip(game.legal_actions(state), probabilities, strict=True))
        children = [(p, visit(game.transition(state, action))) for action, p in outcomes if p > 0]
        return tuple(
            math.fsum(p * value[i] for p, value in children) for i in range(game.num_players)
        )

    return visit(game.initial_state() if start_state is None else start_state)


@dataclass(slots=True, eq=False)
class _Node:
    player: int | None
    actions: tuple = ()
    probabilities: tuple[float, ...] = ()
    information: Hashable | None = None
    children: tuple[_Node, ...] = ()
    payoffs: tuple[float, ...] = ()


def _build_tree(game: Any, profile: Any, budget: TraversalBudget) -> _Node:
    if getattr(game, "supports_exact_enumeration", True) is False:
        raise EvaluationLimitError("this game does not support full-tree exact enumeration")
    observed: dict = {}
    recalls: dict = {}

    def visit(state: Any, own_histories: tuple[tuple, ...]) -> _Node:
        budget.tick()
        player = game.actor(state)
        if player is None:
            return _Node(None, payoffs=checked_payoffs(game, state))
        if player == CHANCE_PLAYER:
            outcomes = checked_chance(game, state)
            return _Node(
                player,
                tuple(a for a, _ in outcomes),
                tuple(p for _, p in outcomes),
                children=tuple(
                    visit(game.transition(state, a), own_histories) for a, _ in outcomes
                ),
            )
        probabilities = checked_probabilities(game, state, profile, observed)
        key = game.information_key(state, player)
        recall_key = (player, key)
        previous = recalls.setdefault(recall_key, own_histories[player])
        if previous != own_histories[player]:
            raise ValueError("exact best response requires perfect recall information sets")
        actions = tuple(game.legal_actions(state))
        children = []
        for action in actions:
            histories = list(own_histories)
            histories[player] = (*histories[player], (key, action))
            children.append(visit(game.transition(state, action), tuple(histories)))
        return _Node(player, actions, probabilities, key, tuple(children))

    return visit(game.initial_state(), tuple(() for _ in range(game.num_players)))


@dataclass(frozen=True)
class BestResponse:
    player: int
    value: float
    actions: dict[Hashable, Hashable]
    nodes: int


def _best_response(root: _Node, player: int, budget: TraversalBudget) -> BestResponse:
    groups: dict[Hashable, list[tuple[_Node, float]]] = {}

    def gather(node: _Node, counterfactual_reach: float) -> None:
        budget.check_time()
        if node.player is None:
            return
        if node.player == player:
            groups.setdefault(node.information, []).append((node, counterfactual_reach))
        for probability, child in zip(node.probabilities, node.children, strict=True):
            gather(child, counterfactual_reach * (1 if node.player == player else probability))

    gather(root, 1.0)
    chosen: dict[Hashable, int] = {}
    solving: set[Hashable] = set()
    values: dict[_Node, float] = {}

    def choose(key: Hashable) -> int:
        if key in chosen:
            return chosen[key]
        if key in solving:
            raise ValueError("cyclic information sets are unsupported by exact best response")
        solving.add(key)
        worlds = groups[key]
        scores = tuple(
            math.fsum(reach * value(node.children[a]) for node, reach in worlds)
            for a in range(len(worlds[0][0].actions))
        )
        chosen[key] = max(range(len(scores)), key=scores.__getitem__)
        solving.remove(key)
        return chosen[key]

    def value(node: _Node) -> float:
        budget.check_time()
        if node in values:
            return values[node]
        if node.player is None:
            result = node.payoffs[player]
        elif node.player == player:
            result = value(node.children[choose(node.information)])
        else:
            result = math.fsum(
                p * value(child) for p, child in zip(node.probabilities, node.children, strict=True)
            )
        values[node] = result
        return result

    result = value(root)
    # Complete the response even at information sets unreachable under its own choices.
    for key in groups:
        choose(key)
    return BestResponse(
        player,
        result,
        {key: groups[key][0][0].actions[index] for key, index in chosen.items()},
        budget.nodes,
    )


def best_response(
    game: Any,
    profile: Any,
    player: int,
    *,
    max_nodes: int = 250_000,
    deadline_seconds: float | None = None,
) -> BestResponse:
    """Exact unilateral response to the other seats' fixed behavioral strategies."""
    if (
        isinstance(player, bool)
        or not isinstance(player, int)
        or not 0 <= player < game.num_players
    ):
        raise ValueError("player must name an existing seat")
    budget = TraversalBudget(max_nodes, deadline_seconds)
    return _best_response(_build_tree(game, profile, budget), player, budget)


@dataclass(frozen=True)
class EvaluationReport:
    policy_values: tuple[float, ...]
    best_response_values: tuple[float, ...]
    nash_conv: float
    exploitability: float | None
    is_zero_sum: bool
    nodes: int
    quality: str = "exact_finite_game"

    def to_dict(self) -> dict:
        return {
            "policy_values": list(self.policy_values),
            "best_response_values": list(self.best_response_values),
            "nash_conv": self.nash_conv,
            "exploitability": self.exploitability,
            "is_zero_sum": self.is_zero_sum,
            "nodes": self.nodes,
            "quality": self.quality,
        }


def evaluate_strategy(
    game: Any,
    profile: Any,
    *,
    max_nodes: int = 250_000,
    deadline_seconds: float | None = None,
) -> EvaluationReport:
    """Exact NashConv; ``NashConv / 2`` is reported only for two-player zero-sum games."""
    budget = TraversalBudget(max_nodes, deadline_seconds)
    root = _build_tree(game, profile, budget)
    zero_sum = True

    def on_policy(node: _Node) -> tuple[float, ...]:
        nonlocal zero_sum
        budget.check_time()
        if node.player is None:
            zero_sum = zero_sum and abs(math.fsum(node.payoffs)) <= 1e-9
            return node.payoffs
        children = [on_policy(child) for child in node.children]
        return tuple(
            math.fsum(p * v[i] for p, v in zip(node.probabilities, children, strict=True))
            for i in range(game.num_players)
        )

    values = on_policy(root)
    responses = tuple(_best_response(root, p, budget).value for p in range(game.num_players))
    nash_conv = math.fsum(max(0.0, br - v) for br, v in zip(responses, values, strict=True))
    return EvaluationReport(
        values,
        responses,
        nash_conv,
        nash_conv / 2 if game.num_players == 2 and zero_sum else None,
        zero_sum,
        budget.nodes,
    )


@dataclass(frozen=True)
class ComparisonReport:
    player: int
    baseline_value: float
    candidate_value: float
    gain: float
    standard_error: float
    samples: int
    seed: int
    quality: str = "sampled_against_fixed_opponents"


def compare_policies(
    game: Any,
    baseline: Any,
    candidate: Any,
    *,
    player: int = 0,
    opponents: Any = None,
    samples: int = 1_000,
    seed: int = 0,
    max_nodes: int = 250_000,
    deadline_seconds: float | None = None,
) -> ComparisonReport:
    """Paired seeded simulations, replacing one seat against fixed opponents.

    Each pair shares chance and per-seat random streams. The standard error is
    for paired payoff differences, not model error or worst-case exploitability.
    """
    if not isinstance(samples, int) or isinstance(samples, bool) or samples < 2:
        raise ValueError("samples must be an integer of at least two")
    if (
        not isinstance(player, int)
        or isinstance(player, bool)
        or not 0 <= player < game.num_players
    ):
        raise ValueError("player must name an existing seat")
    opponents = baseline if opponents is None else opponents
    fixed = tuple(
        policy_for_player(opponents, game.num_players, p) for p in range(game.num_players)
    )
    base_profile = list(fixed)
    candidate_profile = list(fixed)
    base_profile[player] = policy_for_player(baseline, game.num_players, player)
    candidate_profile[player] = policy_for_player(candidate, game.num_players, player)
    budget = TraversalBudget(max_nodes, deadline_seconds)
    observed: tuple[dict, dict] = ({}, {})

    def sample(profile: Any, sample_seed: int, seen: dict) -> float:
        rng = random.Random(sample_seed)
        streams = [random.Random(rng.getrandbits(64)) for _ in range(game.num_players + 1)]
        state = game.initial_state()
        while True:
            budget.tick()
            actor = game.actor(state)
            if actor is None:
                return checked_payoffs(game, state)[player]
            if actor == CHANCE_PLAYER:
                if callable(getattr(game, "sample_chance", None)):
                    state = game.transition(state, game.sample_chance(state, streams[-1]))
                    continue
                outcomes = checked_chance(game, state)
                stream = streams[-1]
            else:
                outcomes = tuple(
                    zip(
                        game.legal_actions(state),
                        checked_probabilities(game, state, profile, seen),
                        strict=True,
                    )
                )
                stream = streams[actor]
            draw = stream.random()
            cumulative = 0.0
            action = outcomes[-1][0]
            for possible, probability in outcomes:
                cumulative += probability
                if draw < cumulative:
                    action = possible
                    break
            state = game.transition(state, action)

    source = random.Random(seed)
    baseline_values: list[float] = []
    candidate_values: list[float] = []
    for _ in range(samples):
        pair_seed = source.getrandbits(64)
        baseline_values.append(sample(base_profile, pair_seed, observed[0]))
        candidate_values.append(sample(candidate_profile, pair_seed, observed[1]))
    differences = [b - a for a, b in zip(baseline_values, candidate_values, strict=True)]
    return ComparisonReport(
        player,
        math.fsum(baseline_values) / samples,
        math.fsum(candidate_values) / samples,
        math.fsum(differences) / samples,
        stdev(differences) / math.sqrt(samples),
        samples,
        seed,
    )
