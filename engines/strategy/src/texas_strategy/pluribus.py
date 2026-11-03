"""Pluribus supplementary Algorithm 1, with a reproducible iteration schedule.

Source: https://noambrown.github.io/papers/19-Science-Superhuman_Supp.pdf pp. 14–18.
The paper schedules discounting, pruning and snapshots by elapsed minutes.
Here schedules count completed iterations, explicitly recorded in artifacts.
Research defaults collect averages immediately; they do not reproduce the
paper's 800-minute averaging warmup, card buckets, integer storage or scale.
"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from math import fsum, isfinite
from random import Random
from time import monotonic
from typing import TYPE_CHECKING

from texas_strategy.cfr import (
    TrainingDiagnostics,
    _BudgetStop,
    _positive_integer,
    _TableEntry,
    chance_distribution,
)
from texas_strategy.policy import legal_information
from texas_strategy.regret import regret_matching

if TYPE_CHECKING:
    from texas_strategy.cfr import Trainer

ALGORITHM_VERSION = "pluribus-iteration-schedule-v1"
SAMPLED_ALGORITHM_VERSION = "pluribus-sampled-average-v1"
MIXTURE_ALGORITHM_VERSION = "pluribus-mixture-average-v1"


@dataclass(frozen=True)
class PluribusConfig:
    strategy_interval: int = 1
    snapshot_interval: int = 100
    average_after: int = 0
    prune_after: int = 200
    discount_interval: int = 10
    discount_until: int = 400
    prune_threshold: float = -300_000_000.0
    regret_floor: float = -310_000_000.0
    average_mode: str = "enumerated"
    average_samples: int = 1
    sampled_average_from: int = 0
    mixture_from: int = 0

    def __post_init__(self):
        if self.average_mode not in ("enumerated", "sampled_uniform", "sampled_mixture"):
            raise ValueError("Invalid Pluribus average mode")
        _positive_integer(self.average_samples, "average_samples")
        _positive_integer(self.sampled_average_from, "sampled_average_from", allow_zero=True)
        _positive_integer(self.mixture_from, "mixture_from", allow_zero=True)
        if self.average_mode != "sampled_mixture" and self.mixture_from != 0:
            raise ValueError("mixture_from requires sampled_mixture averaging")
        if self.average_mode == "enumerated" and (
            self.average_samples != 1 or self.sampled_average_from != 0
        ):
            raise ValueError("Enumerated averaging requires samples=1 and sampled_average_from=0")
        for name in ("strategy_interval", "snapshot_interval", "discount_interval"):
            _positive_integer(getattr(self, name), name)
        for name in ("average_after", "prune_after", "discount_until"):
            _positive_integer(getattr(self, name), name, allow_zero=True)
        for name in ("prune_threshold", "regret_floor"):
            value = getattr(self, name)
            try:
                finite = type(value) in (int, float) and isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"{name} must be finite")
        if not self.regret_floor < self.prune_threshold < 0:
            raise ValueError("Require regret_floor < prune_threshold < 0 in game chip units")

    @property
    def algorithm_version(self):
        if self.average_mode == "sampled_mixture":
            return MIXTURE_ALGORITHM_VERSION
        return ALGORITHM_VERSION if self.average_mode == "enumerated" else SAMPLED_ALGORITHM_VERSION

    def to_dict(self):
        result = asdict(self)
        if self.average_mode != "sampled_mixture":
            del result["mixture_from"]
        if self.average_mode == "enumerated":
            for name in ("average_mode", "average_samples", "sampled_average_from"):
                del result[name]
        return result

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError("Invalid Pluribus settings")
        return cls(**value)


@dataclass(frozen=True)
class PluribusDiagnostics(TrainingDiagnostics):
    pruned_actions: int
    full_traversals: int
    discounted_iterations: int
    snapshot_iterations: int


def train(
    trainer: "Trainer",
    iterations: int,
    *,
    max_nodes: int | None,
    deadline_seconds: float | None,
    should_cancel: Callable[[], bool] | None,
):
    _positive_integer(iterations, "iterations", allow_zero=True)
    if max_nodes is not None:
        _positive_integer(max_nodes, "max_nodes", allow_zero=True)
    if deadline_seconds is not None and (
        type(deadline_seconds) not in (int, float)
        or not isfinite(deadline_seconds)
        or deadline_seconds < 0
    ):
        raise ValueError("deadline_seconds must be finite and nonnegative")
    game, rng, config = trainer.game, trainer._rng, trainer.pluribus
    started = monotonic()
    deadline = None if deadline_seconds is None else started + deadline_seconds
    nodes = completed = pruned = full = discounted = snapshots = 0
    reason = "iterations"
    sampler = getattr(game, "sample_chance_outcome", None)
    if sampler is not None and not callable(sampler):
        raise ValueError("sample_chance_outcome must be callable")

    def check():
        if should_cancel is not None and should_cancel():
            raise _BudgetStop("cancelled")
        if deadline is not None and monotonic() >= deadline:
            raise _BudgetStop("deadline")

    def visit():
        nonlocal nodes
        check()
        if max_nodes is not None and nodes >= max_nodes:
            raise _BudgetStop("node_budget")
        nodes += 1

    def sample(probabilities, source=rng):
        threshold, cumulative = source.random(), 0.0
        for index, probability in enumerate(probabilities):
            cumulative += probability
            if threshold < cumulative:
                return index
        return len(probabilities) - 1

    def chance(state, source=rng):
        if sampler is None:
            outcomes = chance_distribution(game, state)
            return outcomes[sample(tuple(p for _, p in outcomes), source)][0]
        action, probability = sampler(state, source)
        if (
            not isinstance(action, str)
            or not action
            or type(probability) not in (int, float)
            or not isfinite(probability)
            or not 0 < probability <= 1
        ):
            raise ValueError("Invalid exact chance sampler outcome")
        return action

    store = getattr(trainer, "_store", None)
    for _ in range(iterations):
        saved_rng = rng.getstate()
        rounds = {}
        if store is not None:
            from texas_strategy.training_store import _IterationStore

            working = _IterationStore(store)
        else:
            working = {}
        t = trainer.iterations + 1

        def copy_entry(key, working=working, rounds=rounds):
            if key not in working:
                old = trainer._table[key]
                working[key] = _TableEntry(old.actions, old.regrets.copy(), old.strategy_sum.copy())
                rounds[key] = trainer._betting_rounds[key]
            return working[key]

        @contextmanager
        def decision(state, working=working, rounds=rounds, copy_entry=copy_entry):
            key, actions = legal_information(game, state)
            stage = game.betting_round(state)
            if type(stage) is not int or not 0 <= stage <= game.final_betting_round:
                raise ValueError("Invalid betting round")
            if store is not None:
                with working.entry(key, actions, stage) as entry:
                    yield entry, regret_matching(entry.regrets)
                return
            if key not in working:
                if key in trainer._table:
                    copy_entry(key)
                else:
                    working[key] = _TableEntry(actions, [0.0] * len(actions), [0.0] * len(actions))
            entry = working[key]
            if entry.actions != actions or rounds.get(key, stage) != stage:
                raise ValueError("Information set changes legal actions or betting round")
            rounds[key] = stage
            yield entry, regret_matching(entry.regrets)

        def average(state, player):
            visit()
            actor = game.actor(state)
            if (
                actor is None
                or game.betting_round(state) > 0
                or player not in game.active_players(state)
            ):
                return
            if actor == -1:
                average(game.transition(state, chance(state)), player)
                return
            with decision(state) as (entry, policy):
                if actor == player:
                    index = sample(policy)
                    entry.strategy_sum[index] += 1
                    average(game.transition(state, entry.actions[index]), player)
                else:
                    for action in entry.actions:
                        average(game.transition(state, action), player)

        def sampled_average(player, source, t=t):
            state, weight = game.initial_state(), 1.0
            mixture = config.average_mode == "sampled_mixture" and t >= config.mixture_from
            passive_path = mixture and source.randrange(2) == 0
            uniform_reach = passive_reach = 1.0
            while True:
                visit()
                actor = game.actor(state)
                if (
                    actor is None
                    or game.betting_round(state) > 0
                    or player not in game.active_players(state)
                ):
                    return
                if actor == -1:
                    state = game.transition(state, chance(state, source))
                    continue
                with decision(state) as (entry, policy):
                    if actor == player:
                        # Conditional expectation of the sampled own-action counter.
                        for index, probability in enumerate(policy):
                            value = (
                                entry.strategy_sum[index]
                                + weight * probability / config.average_samples
                            )
                            if not isfinite(value):
                                raise ValueError("Nonfinite sampled average update")
                            entry.strategy_sum[index] = value
                        index = sample(policy, source)
                    else:
                        # Only past opponent proposals are corrected; chance and own reach stay.
                        passive = (
                            next(
                                (
                                    entry.actions.index(a)
                                    for a in ("check", "call")
                                    if a in entry.actions
                                ),
                                None,
                            )
                            if mixture
                            else None
                        )
                        index = (
                            passive
                            if passive_path and passive is not None
                            else source.randrange(len(entry.actions))
                        )
                        if mixture:
                            uniform_reach /= len(entry.actions)
                            passive_reach *= (
                                1 / len(entry.actions) if passive is None else int(index == passive)
                            )
                            # Correct the complete mixture prefix, not the sampled branch alone.
                            density = (uniform_reach + passive_reach) / 2
                            if density <= 0:
                                raise ValueError("Nonfinite sampled average importance weight")
                            weight = 1 / density
                        else:
                            weight *= len(entry.actions)
                        if not isfinite(weight):
                            raise ValueError("Nonfinite sampled average importance weight")
                    state = game.transition(state, entry.actions[index])

        def traverse(state, player, prune):
            nonlocal pruned
            visit()
            actor = game.actor(state)
            if actor is None:
                values = game.terminal_payoffs(state)
                if len(values) != game.num_players or any(
                    type(v) not in (int, float) or not isfinite(v) for v in values
                ):
                    raise ValueError("Terminal payoffs must be finite and player-aligned")
                return float(values[player])
            if actor == -1:
                return traverse(game.transition(state, chance(state)), player, prune)
            with decision(state) as (entry, policy):
                if actor != player:
                    return traverse(
                        game.transition(state, entry.actions[sample(policy)]), player, prune
                    )
                values = {}
                for i, action in enumerate(entry.actions):
                    child = game.transition(state, action)
                    # Never remove positive policy mass, river actions or immediate payoffs.
                    if (
                        prune
                        and entry.regrets[i] <= config.prune_threshold
                        and policy[i] == 0
                        and game.betting_round(state) < game.final_betting_round
                        and game.actor(child) is not None
                    ):
                        pruned += 1
                        continue
                    values[i] = traverse(child, player, prune)
                value = fsum(policy[i] * v for i, v in values.items())
                for i, v in values.items():
                    entry.regrets[i] = max(config.regret_floor, entry.regrets[i] + v - value)
                return value

        try:
            if store is not None:
                store.begin(trainer.iterations)
            for player in range(game.num_players):
                if t > config.average_after and t % config.strategy_interval == 0:
                    if config.average_mode == "enumerated":
                        average(game.initial_state(), player)
                    elif t >= config.sampled_average_from:
                        for sample_index in range(config.average_samples):
                            # Stable domain separation; averaging never advances regret RNG.
                            version = (
                                SAMPLED_ALGORITHM_VERSION
                                if config.average_mode == "sampled_mixture"
                                and t < config.mixture_from
                                else config.algorithm_version
                            )
                            source = Random(f"{version}:{trainer.seed}:{t}:{player}:{sample_index}")
                            sampled_average(player, source)
                prune = t > config.prune_after and rng.random() >= 0.05
                full += int(not prune)
                traverse(game.initial_state(), player, prune)
            discount = t < config.discount_until and t % config.discount_interval == 0
            snapshot = (
                t > config.average_after
                and (t - config.average_after) % config.snapshot_interval == 0
            )

            def transform(entry, stage, *, discount=discount, snapshot=snapshot, t=t):
                check()
                if discount:
                    epoch = t // config.discount_interval
                    factor = epoch / (epoch + 1)
                    entry.regrets = [v * factor for v in entry.regrets]
                    entry.strategy_sum = [v * factor for v in entry.strategy_sum]
                if snapshot and stage > 0:
                    entry.strategy_sum = [
                        v + p
                        for v, p in zip(
                            entry.strategy_sum, regret_matching(entry.regrets), strict=True
                        )
                    ]
                if any(not isfinite(v) for v in entry.regrets + entry.strategy_sum):
                    raise ValueError("Nonfinite Pluribus training update")

            if store is not None:
                # Stream old rows inside the same transaction; never copy the full table.
                if discount or snapshot:
                    for key, entry, stage in store.scan():
                        if key not in working:
                            transform(entry, stage)
                            if any(entry.regrets) or any(entry.strategy_sum):
                                store.set_entry(key, entry, stage)
                for key, entry, stage in working.scan():
                    transform(entry, stage)
                    if any(entry.regrets) or any(entry.strategy_sum):
                        store.set_entry(key, entry, stage)
            else:
                if discount or snapshot:
                    for key in trainer._table.keys() | working.keys():
                        entry = copy_entry(key) if key not in working else working[key]
                        transform(entry, rounds[key])
                updates = {
                    key: entry
                    for key, entry in working.items()
                    if any(entry.regrets) or any(entry.strategy_sum)
                }
                if any(
                    not isfinite(v)
                    for entry in updates.values()
                    for v in entry.regrets + entry.strategy_sum
                ):
                    raise ValueError("Nonfinite Pluribus training update")
            check()
            if store is not None:
                store.commit(trainer, t)
        except _BudgetStop as exc:
            if store is not None:
                store.rollback()
            rng.setstate(saved_rng)
            reason = str(exc)
            break
        except Exception:
            if store is not None:
                store.rollback()
            rng.setstate(saved_rng)
            raise
        finally:
            if store is not None:
                working.close()
        if store is None:
            trainer._table.update(updates)
            trainer._betting_rounds.update({key: rounds[key] for key in updates})
        trainer.iterations += 1
        completed += 1
        discounted += int(discount)
        snapshots += int(snapshot)
    return PluribusDiagnostics(
        completed,
        trainer.iterations,
        nodes,
        monotonic() - started,
        reason,
        len(trainer._table),
        pruned,
        full,
        discounted,
        snapshots,
    )
