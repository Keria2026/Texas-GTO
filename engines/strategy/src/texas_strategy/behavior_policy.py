"""Regularized public-action models; probabilities are behavior, never action value."""

import json
from math import fsum, isfinite, sqrt
from random import Random

from texas_strategy.evaluation import checked_payoffs, checked_probabilities
from texas_strategy.mushroom_abstraction import context_action_map
from texas_strategy.policy import PolicyEntry
from texas_strategy.search import SearchCoverageError


def family(action):
    return "fold" if action == "fold" else "passive" if action in ("check", "call") else "raise"


def context(state, actor, button, actions):
    """Identical public features for recorded decisions and sampled engine states."""
    call = min(state.stacks[actor], max(0, state.current_bet - state.street_bets[actor]))
    pot = sum(state.contributions)
    odds = call / max(1, pot + call)
    spr = state.stacks[actor] / max(1, pot)
    legal = tuple(sorted({family(a) for a in actions}))
    coarse = (state.street, bool(call), legal)
    detailed = (
        *coarse,
        sum(not f for f in state.folded),
        (actor - button) % len(state.stacks),
        sum(odds > x for x in (0.2, 0.4)),
        sum(spr > x for x in (2, 6)),
    )
    return coarse, detailed


def key(scope, features):
    return json.dumps([scope, features], separators=(",", ":"))


def fit(samples):
    counts = {}
    for sample in samples:
        for scope, features in (
            ("population", sample["coarse"]),
            ("population", sample["detailed"]),
            ([sample["source"], sample["player"]], sample["coarse"]),
            ([sample["source"], sample["player"]], sample["detailed"]),
        ):
            row = counts.setdefault(key(scope, features), {})
            row[sample["label"]] = row.get(sample["label"], 0) + 1
    return counts


def predict(counts, source, player, features):
    coarse, detailed = features
    legal = coarse[2]
    distribution = dict.fromkeys(legal, 1 / len(legal))
    personal = population = 0
    for scope, feature in (
        ("population", coarse),
        ("population", detailed),
        ([source, player], coarse),
        ([source, player], detailed),
    ):
        row = counts.get(key(scope, feature), {})
        n = sum(row.get(a, 0) for a in legal)
        if scope == "population":
            population = max(population, n)
        else:
            personal = max(personal, n)
        distribution = {a: (row.get(a, 0) + 16 * distribution[a]) / (n + 16) for a in legal}
    return distribution, personal, population


class BehaviorPolicy:
    """A pinned, released model conditions opponents only; hero keeps the blueprint.

    The finite search still optimizes hidden continuation choices for every player.
    This is a model-conditioned approximation, not a multiplayer equilibrium claim.
    """

    def __init__(self, blueprint, counts, *, source, players, hero, button):
        self.blueprint, self.counts = blueprint, counts
        self.source, self.players, self.hero, self.button = source, players, hero, button
        self.metadata = blueprint.metadata
        self.applied = 0
        self.max_weight = 0.0

    @property
    def fallback_count(self):
        return self.blueprint.fallback_count

    def lookup(self, player, information_key):
        return self.blueprint.lookup(player, information_key)

    def contains(self, game, state):
        return self.blueprint.contains(game, state)

    def public_entry(self, entry, physical, state, actions, *, actor=None, button=None):
        if physical == self.hero:
            return entry
        actor = physical if actor is None else actor
        features = context(state, actor, self.button if button is None else button, actions)
        predicted, personal, population = predict(
            self.counts, self.source, self.players[physical], features
        )
        n = personal or population
        if n < 8:
            return entry
        weight = (0.25 if personal else 0.10) * n / (n + 40)
        grouped = {
            a: fsum(
                p
                for label, p in zip(actions, entry.probabilities, strict=True)
                if family(label) == a
            )
            for a in predicted
        }
        values = []
        for label, probability in zip(actions, entry.probabilities, strict=True):
            group = family(label)
            share = (
                probability / grouped[group]
                if grouped[group]
                else (1 / sum(family(a) == group for a in actions))
            )
            values.append((1 - weight) * probability + weight * predicted[group] * share)
        self.applied += 1
        self.max_weight = max(self.max_weight, weight)
        return PolicyEntry(actions, tuple(v / fsum(values) for v in values))

    def fixed_entry(self, game, state):
        player = game.actor(state)
        physical = (player + game.base_game.button) % game.num_players
        if physical == self.hero:
            return None
        table = state.underlying.table
        actor = table.participants.index(physical)
        actions = tuple(context_action_map(table.hand.game, table.hand.state))
        _, personal, population = predict(
            self.counts,
            self.source,
            self.players[physical],
            context(table.hand.state, actor, table.hand.game.button, actions),
        )
        if (personal or population) < 8:
            return None
        if not self.blueprint.contains(game, state):
            raise SearchCoverageError("Opponent behavior requires covered blueprint actions")
        base = PolicyEntry(actions, checked_probabilities(game, state, self.blueprint))
        adapted = self.public_entry(
            base, physical, table.hand.state, actions, actor=actor, button=table.hand.game.button
        )
        return adapted if adapted is not base else None

    def probabilities(self, game, state):
        if not self.blueprint.contains(game, state):
            raise SearchCoverageError("Behavior rollout requires covered blueprint actions")
        adapted = self.fixed_entry(game, state)
        return (
            adapted.probabilities if adapted else checked_probabilities(game, state, self.blueprint)
        )


def compare_actions(game, sampler, policy, hero, baseline, candidate, *, budget, seed, samples=32):
    """Independent paired full-horizon rollouts of the two current-action mixtures.

    The confidence bound is Monte Carlo error under this opponent model only;
    it is not protection against a wrong model or an infinite-session EV claim.
    """
    rng = Random(seed)
    observations = []

    def rollout(state, probabilities, draw_seed):
        draw = Random(draw_seed)
        action = draw.choices(baseline.actions, weights=probabilities)[0]
        state = game.transition(state, action)
        while game.actor(state) is not None:
            budget.tick()
            actor = game.actor(state)
            if actor == -1:
                action, _ = game.sample_chance_outcome(state, draw)
            else:
                values = checked_probabilities(game, state, policy)
                action = draw.choices(game.legal_actions(state), weights=values)[0]
            state = game.transition(state, action)
        return checked_payoffs(game, state)[hero]

    if baseline.actions != candidate.actions:
        raise ValueError("Evaluation actions differ")
    for _ in range(samples * 4):
        budget.tick()
        state, weight = sampler(rng)
        if not isfinite(weight) or weight < 0:
            raise ValueError("Evaluation importance weights must be finite and nonnegative")
        if state is None or weight <= 0:
            continue
        if game.actor(state) != hero or tuple(game.legal_actions(state)) != baseline.actions:
            raise ValueError("Evaluation world differs from the target decision")
        draw_seed = rng.getrandbits(64)
        delta = rollout(state, candidate.probabilities, draw_seed) - rollout(
            state, baseline.probabilities, draw_seed
        )
        observations.append((weight, delta))
        if len(observations) == samples:
            break
    if not observations:
        return {
            "accepted": False,
            "samples": 0,
            "effective_samples": 0.0,
            "delta_points": 0.0,
            "standard_error": 0.0,
            "lower_bound": 0.0,
        }
    scale = max(w for w, _ in observations)
    weights = [w / scale for w, _ in observations]
    total = fsum(weights)
    normalized = [w / total for w in weights]
    effective = 1 / fsum(w * w for w in normalized)
    mean = fsum(w * item[1] for w, item in zip(normalized, observations, strict=True))
    variance = fsum(
        w * w * (item[1] - mean) ** 2 for w, item in zip(normalized, observations, strict=True)
    )
    error = sqrt(variance * effective / (effective - 1)) if effective > 1 else 0.0
    lower = mean - 1.96 * error
    budget.check_time()
    return {
        "accepted": effective >= 16 and lower > 0,
        "samples": len(observations),
        "effective_samples": effective,
        "delta_points": mean,
        "standard_error": error,
        "lower_bound": lower,
    }
