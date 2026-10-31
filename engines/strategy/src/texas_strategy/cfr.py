"""Finite-game CFR and importance-corrected external-sampling MCCFR.

This research solver is inspired by the blueprint stage of Pluribus, not a
reproduction of its abstractions, pruning, optimizations or training scale.
Games must have perfect recall. Multiplayer training carries no Nash guarantee.

Each iteration freezes the policy and updates every player. Exact CFR weights
regrets by chance and other players' reach, and averages by the acting player's
own reach. Linear weighting multiplies both updates by the global iteration.

Sampled traversal enumerates the traverser's actions and samples chance and
opponents. Opponents use full-support q=(1-epsilon)*pi+epsilon/|A|. Prefix pi/q
corrects regret updates; suffix pi/q corrects sampled values. For averaging,
ownReach / sampledExternalPrefixProbability corrects visitation to the *same*
per-history own-reach sum as full CFR. Multiplying own reach twice, or using
uncorrected externally sampled visits, would produce a different average.
The full-support proposal is necessary even when target action probability is
zero. This estimator can have high variance on long paths; it is deliberately
an auditable tabular baseline, not a production six-player training claim.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from math import fsum, isclose, isfinite
from pathlib import Path
from random import Random
from time import monotonic
from typing import Any, Literal

from texas_strategy.policy import InformationSet, PolicyEntry, TabularPolicy, legal_information
from texas_strategy.regret import regret_matching

ALGORITHM_VERSION = "tabular-cfr-v1"


@dataclass(frozen=True)
class TrainingDiagnostics:
    completed_iterations: int
    total_iterations: int
    nodes: int
    elapsed_seconds: float
    stop_reason: str
    information_sets: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _TableEntry:
    actions: tuple[str, ...]
    regrets: list[float]
    strategy_sum: list[float]


class _BudgetStop(Exception):
    pass


def _positive_integer(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise ValueError(f"{name} must be {'nonnegative' if allow_zero else 'positive'} integer")
    return value


def chance_distribution(game: Any, state: Any) -> tuple[tuple[str, float], ...]:
    outcomes = tuple(game.chance_outcomes(state))
    if not outcomes:
        raise ValueError("Chance node has no outcomes")
    actions = []
    probabilities = []
    for action, probability in outcomes:
        if not isinstance(action, str) or not action:
            raise ValueError("Chance actions must be nonempty strings")
        if (
            type(probability) not in (float, int)
            or not isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise ValueError("Chance probabilities must be finite and nonnegative")
        actions.append(action)
        probabilities.append(float(probability))
    if len(set(actions)) != len(actions):
        raise ValueError("Chance actions must be distinct")
    if not isclose(fsum(probabilities), 1.0, rel_tol=0, abs_tol=1e-10):
        raise ValueError("Chance probabilities must sum to one")
    return tuple((a, p) for a, p in zip(actions, probabilities, strict=True) if p > 0)


class Trainer:
    def __init__(
        self,
        game: Any,
        *,
        algorithm: Literal["cfr", "mccfr"] = "cfr",
        weighting: Literal["uniform", "linear"] = "uniform",
        seed: int = 0,
        exploration: float = 0.06,
    ) -> None:
        _positive_integer(game.num_players, "num_players")
        if algorithm not in ("cfr", "mccfr") or weighting not in ("uniform", "linear"):
            raise ValueError("Unsupported algorithm or iteration weighting")
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        if type(exploration) not in (int, float) or not isfinite(exploration):
            raise ValueError("exploration must be finite")
        if not 0 < exploration <= 1:
            raise ValueError("Full-support exploration must be in (0, 1]")
        self.game = game
        self.algorithm = algorithm
        self.weighting = weighting
        self.seed = seed
        self.exploration = float(exploration)
        self.iterations = 0
        self._rng = Random(seed)
        self._table: dict[InformationSet, _TableEntry] = {}

    def average_policy(self, *, missing: Literal["uniform", "error"] = "uniform") -> TabularPolicy:
        entries = {}
        for key, entry in self._table.items():
            maximum = max(entry.strategy_sum)
            if maximum > 0:
                scaled = [value / maximum for value in entry.strategy_sum]
                total = fsum(scaled)
                probabilities = tuple(value / total for value in scaled)
            else:
                # Opponent-only sampled visits need not accumulate an average.
                # Do not label their uniform fallback as trained coverage.
                continue
            entries[key] = PolicyEntry(entry.actions, probabilities)
        return TabularPolicy(entries, missing=missing)

    def train(
        self,
        iterations: int,
        *,
        max_nodes: int | None = None,
        deadline_seconds: float | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> TrainingDiagnostics:
        """Train complete iterations within budgets shared by this invocation.

        ``max_nodes`` counts all recursive visits across all players and requested
        iterations, including discarded work. ``deadline_seconds`` is elapsed
        time for this call; checked at each node, so a single game method is not
        preempted. Aborted iterations roll back RNG and every table update.
        Subsequent calls resume the last complete iteration deterministically.
        """
        _positive_integer(iterations, "iterations", allow_zero=True)
        if max_nodes is not None:
            _positive_integer(max_nodes, "max_nodes", allow_zero=True)
        if deadline_seconds is not None and (
            type(deadline_seconds) not in (float, int)
            or not isfinite(deadline_seconds)
            or deadline_seconds < 0
        ):
            raise ValueError("deadline_seconds must be finite and nonnegative")
        start = monotonic()
        # Optional game-owned exact sampler avoids materializing every chance
        # outcome. Its returned probability is needed by the unbiased average.
        # As with terminal_payoffs, correctness of this game primitive belongs
        # to the rules implementation and its distribution tests.
        chance_sampler = getattr(self.game, "sample_chance_outcome", None)
        if chance_sampler is not None and not callable(chance_sampler):
            raise ValueError("sample_chance_outcome must be callable")
        deadline = None if deadline_seconds is None else start + deadline_seconds
        nodes = 0
        completed = 0
        stop_reason = "iterations"

        def visit() -> None:
            nonlocal nodes
            if should_cancel is not None and should_cancel():
                raise _BudgetStop("cancelled")
            if max_nodes is not None and nodes >= max_nodes:
                raise _BudgetStop("node_budget")
            if deadline is not None and monotonic() >= deadline:
                raise _BudgetStop("deadline")
            nodes += 1

        frozen: dict[InformationSet, tuple[float, ...]] = {}
        deltas: dict[InformationSet, _TableEntry] = {}
        for _ in range(iterations):
            rng_state = self._rng.getstate()
            frozen.clear()
            deltas.clear()
            weight = self.iterations + 1 if self.weighting == "linear" else 1

            def decision(state: Any) -> tuple[InformationSet, _TableEntry, tuple[float, ...]]:
                key, actions = legal_information(self.game, state)
                if key not in deltas:
                    prior = self._table.get(key)
                    if prior is not None and prior.actions != actions:
                        raise ValueError("Information set has inconsistent legal actions")
                    deltas[key] = _TableEntry(actions, [0.0] * len(actions), [0.0] * len(actions))
                    frozen[key] = regret_matching(prior.regrets if prior else [0.0] * len(actions))
                elif deltas[key].actions != actions:
                    raise ValueError("Information set has inconsistent legal actions")
                return key, deltas[key], frozen[key]

            def payoff(state: Any, traverser: int) -> float:
                values = self.game.terminal_payoffs(state)
                if len(values) != self.game.num_players or any(
                    type(value) not in (int, float) or not isfinite(value) for value in values
                ):
                    raise ValueError("Terminal payoffs must be finite and player-aligned")
                return float(values[traverser])

            def exact(
                state: Any, traverser: int, own: float, external: float, *, iteration_weight=weight
            ) -> float:
                visit()
                actor = self.game.actor(state)
                if actor is None:
                    return payoff(state, traverser)
                if actor == -1:
                    return fsum(
                        probability
                        * exact(
                            self.game.transition(state, action),
                            traverser,
                            own,
                            external * probability,
                        )
                        for action, probability in chance_distribution(self.game, state)
                    )
                _, entry, policy = decision(state)
                values = tuple(
                    exact(
                        self.game.transition(state, action),
                        traverser,
                        own * probability if actor == traverser else own,
                        external if actor == traverser else external * probability,
                    )
                    for action, probability in zip(entry.actions, policy, strict=True)
                )
                value = fsum(p * v for p, v in zip(policy, values, strict=True))
                if actor == traverser:
                    for index, probability in enumerate(policy):
                        entry.regrets[index] += (
                            iteration_weight * external * (values[index] - value)
                        )
                        entry.strategy_sum[index] += iteration_weight * own * probability
                return value

            def sample(probabilities: tuple[float, ...]) -> int:
                threshold = self._rng.random()
                cumulative = 0.0
                for index, probability in enumerate(probabilities):
                    cumulative += probability
                    if threshold < cumulative:
                        return index
                return len(probabilities) - 1

            def sampled(
                state: Any,
                traverser: int,
                own: float,
                sampling_reach: float,
                ratio: float,
                *,
                iteration_weight=weight,
            ) -> float:
                visit()
                actor = self.game.actor(state)
                if actor is None:
                    return payoff(state, traverser)
                if actor == -1:
                    if chance_sampler is None:
                        outcomes = chance_distribution(self.game, state)
                        index = sample(tuple(p for _, p in outcomes))
                        action, probability = outcomes[index]
                    else:
                        action, probability = chance_sampler(state, self._rng)
                        if (
                            not isinstance(action, str)
                            or not action
                            or type(probability) not in (int, float)
                            or not isfinite(probability)
                            or not 0 < probability <= 1
                        ):
                            raise ValueError("Invalid exact chance sampler outcome")
                    return sampled(
                        self.game.transition(state, action),
                        traverser,
                        own,
                        sampling_reach * probability,
                        ratio,
                    )
                _, entry, policy = decision(state)
                if actor != traverser:
                    proposal = tuple(
                        (1 - self.exploration) * p + self.exploration / len(policy) for p in policy
                    )
                    index = sample(proposal)
                    importance = policy[index] / proposal[index]
                    return importance * sampled(
                        self.game.transition(state, entry.actions[index]),
                        traverser,
                        own,
                        sampling_reach * proposal[index],
                        ratio * importance,
                    )
                if sampling_reach <= 0:
                    raise ValueError("Sampling reach underflow; use a smaller game abstraction")
                values = tuple(
                    sampled(
                        self.game.transition(state, action),
                        traverser,
                        own * probability,
                        sampling_reach,
                        ratio,
                    )
                    for action, probability in zip(entry.actions, policy, strict=True)
                )
                value = fsum(p * v for p, v in zip(policy, values, strict=True))
                for index, probability in enumerate(policy):
                    entry.regrets[index] += iteration_weight * ratio * (values[index] - value)
                    entry.strategy_sum[index] += (
                        iteration_weight * own / sampling_reach * probability
                    )
                return value

            try:
                for traverser in range(self.game.num_players):
                    root = self.game.initial_state()
                    if self.algorithm == "cfr":
                        exact(root, traverser, 1.0, 1.0)
                    else:
                        sampled(root, traverser, 1.0, 1.0, 1.0)
                updates = {}
                for key, delta in deltas.items():
                    # External sampling sees many opponents' information sets
                    # that receive no update in this iteration. Persisting a
                    # fresh all-zero table entry is exactly equivalent to an
                    # absent uniform entry, and wastes most Hold'em memory.
                    if not any(delta.regrets) and not any(delta.strategy_sum):
                        continue
                    prior = self._table.get(key)
                    regrets = [
                        value + (prior.regrets[i] if prior else 0.0)
                        for i, value in enumerate(delta.regrets)
                    ]
                    sums = [
                        value + (prior.strategy_sum[i] if prior else 0.0)
                        for i, value in enumerate(delta.strategy_sum)
                    ]
                    if any(not isfinite(value) for value in regrets + sums):
                        raise ValueError(
                            "Nonfinite training update; use a smaller game abstraction"
                        )
                    updates[key] = _TableEntry(delta.actions, regrets, sums)
                # Check elapsed time once more before the only mutation point.
                if should_cancel is not None and should_cancel():
                    raise _BudgetStop("cancelled")
                if deadline is not None and monotonic() >= deadline:
                    raise _BudgetStop("deadline")
            except _BudgetStop as exc:
                self._rng.setstate(rng_state)
                stop_reason = str(exc)
                break
            except Exception:
                self._rng.setstate(rng_state)
                raise
            self._table.update(updates)
            self.iterations += 1
            completed += 1
        return TrainingDiagnostics(
            completed, self.iterations, nodes, monotonic() - start, stop_reason, len(self._table)
        )

    def save_checkpoint(self, path: str | Path) -> None:
        from texas_strategy.blueprint import save_checkpoint

        save_checkpoint(self, path)

    @classmethod
    def load_checkpoint(
        cls, game: Any, path: str | Path, *, max_bytes: int = 64 * 1024 * 1024
    ) -> "Trainer":
        from texas_strategy.blueprint import load_checkpoint

        return load_checkpoint(game, path, max_bytes=max_bytes)
