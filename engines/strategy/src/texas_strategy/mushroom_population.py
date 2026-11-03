"""One persistent policy trained across an explicit distribution of public openings.

Eight stable model seats include explicitly waiting seats. Root selection is an
ordinary chance event, so the existing Pluribus estimator and checkpoint RNG apply.
This expands training support; it does not establish coverage of an observed hand.
"""

import json
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256

from texas_game_core.mushroom_game import MushroomSessionGame

from .blueprint import _canonical, _nonfinite, _object
from .mushroom_abstraction import MushroomContextGame, MushroomContextState

VERSION = "mushroom-population-v1"
MAX_ROOTS = 1024
SAFE = (1 << 53) - 1


@dataclass(frozen=True)
class PopulationState:
    population: str
    root: int | None = None
    underlying: MushroomContextState | None = None


def _amounts(values):
    if (
        not isinstance(values, (list, tuple))
        or len(values) != 8
        or any(type(v) is not int or not 0 <= v <= SAFE for v in values)
    ):
        raise ValueError("Opening requires eight explicit nonnegative integer amounts")
    return tuple(values)


def _root(value):
    if not isinstance(value, dict) or set(value) != {
        "weight",
        "stacks",
        "outstanding",
        "waiting_for_top_up",
        "button",
    }:
        raise ValueError("Invalid population root fields")
    if type(value["weight"]) is not int or not 1 <= value["weight"] <= 1_000_000:
        raise ValueError("Population weights must be positive bounded integers")
    if type(value["button"]) is not int or not 0 <= value["button"] < 8:
        raise ValueError("Population button must be an explicit model seat")
    stacks, outstanding = _amounts(value["stacks"]), _amounts(value["outstanding"])
    waiting = value["waiting_for_top_up"]
    if (
        not isinstance(waiting, (list, tuple))
        or any(type(p) is not int or not 0 <= p < 8 for p in waiting)
        or tuple(waiting) != tuple(sorted(set(waiting)))
        or sum(stacks) + sum(outstanding) > SAFE
    ):
        raise ValueError("Invalid population waiting seats or funding")
    return {
        "weight": value["weight"],
        "stacks": stacks,
        "outstanding": outstanding,
        "waiting_for_top_up": tuple(waiting),
        "button": value["button"],
    }


class MushroomPopulationGame:
    game_id = "mushroom"
    num_players = 8
    supports_exact_enumeration = False
    abstraction_version = VERSION

    def __init__(self, roots, *, horizon=2, warm_start=None):
        if type(horizon) is not int or not 1 <= horizon <= 4:
            raise ValueError("Population horizon must be between one and four hands")
        if not isinstance(roots, (tuple, list)) or not 1 <= len(roots) <= MAX_ROOTS:
            raise ValueError("A bounded explicit population is required")
        self._roots = tuple(_root(root) for root in roots)
        signatures = [
            _canonical({k: v for k, v in r.items() if k != "weight"}) for r in self._roots
        ]
        if len(signatures) != len(set(signatures)):
            raise ValueError("Duplicate population openings must not silently change weights")
        if warm_start is not None:
            if (
                not isinstance(warm_start, dict)
                or set(warm_start) != {"previous_context", "source_iterations", "schedule"}
                or not isinstance(warm_start["previous_context"], str)
                or not warm_start["previous_context"]
                or type(warm_start["source_iterations"]) is not int
                or warm_start["source_iterations"] < 0
                or warm_start["schedule"] != "continue_total_iterations"
            ):
                raise ValueError("Invalid population warm-start provenance")
        self.horizon = horizon
        self.checkpoint_context = _canonical(
            {
                "version": VERSION,
                "horizon": horizon,
                "roots": self._roots,
                "warm_start": warm_start,
            }
        ).decode()
        self._identity = sha256(self.checkpoint_context.encode()).hexdigest()
        self._games = OrderedDict()
        self._cumulative = []
        total = 0
        self.rules_version = None
        for index, root in enumerate(self._roots):
            game = self._game(index)
            if self.rules_version is None:
                self.rules_version = game.rules_version
                self.final_betting_round = game.final_betting_round
            if game.rules_version != self.rules_version:
                raise ValueError("Population roots must share one rules family")
            opening = game.initial_state().underlying.table
            if opening.hand is None or not 4 <= len(opening.participants) <= 8:
                raise ValueError(
                    "Every population root must open a funded four-to-eight-player hand"
                )
            total += root["weight"]
            self._cumulative.append(total)
        self._total_weight = total
        if warm_start is not None:
            if not any(
                self._game(i).checkpoint_context == warm_start["previous_context"]
                for i in range(len(self._roots))
            ):
                raise ValueError("Warm-start opening is absent from the population")

    @property
    def roots(self):
        return json.loads(self.checkpoint_context)["roots"]

    @property
    def warm_start(self):
        return json.loads(self.checkpoint_context)["warm_start"]

    @classmethod
    def default(cls, *, horizon=2):
        # Exact legacy opening first; half the root mass remains eight-player training.
        masks = [255] + [m for m in range(256) if 4 <= m.bit_count() < 8]
        seat_weights = {4: 4, 5: 5, 6: 10, 7: 35, 8: 1120}
        varied_cash = (400, 800, 1600, 3200, 6400, 12800, 800, 1600)
        roots = []
        for mask in masks:
            active = tuple(p for p in range(8) if mask & (1 << p))
            waiting = tuple(p for p in range(8) if p not in active)
            for profile in range(3):
                roots.append(
                    {
                        "weight": seat_weights[len(active)] * (4 if profile == 0 else 1),
                        "stacks": tuple(
                            (800 if profile == 0 else varied_cash[p]) if p in active else 0
                            for p in range(8)
                        ),
                        "outstanding": tuple(
                            20 * (p + 1) if profile == 2 and p in active else 0 for p in range(8)
                        ),
                        "waiting_for_top_up": waiting,
                        "button": 0,
                    }
                )
        return cls(roots, horizon=horizon)

    @classmethod
    def from_context(cls, context):
        if not isinstance(context, str) or len(context.encode()) > 1024 * 1024:
            raise ValueError("Invalid population context")
        try:
            value = json.loads(context, object_pairs_hook=_object, parse_constant=_nonfinite)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("Invalid population context") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "horizon", "roots", "warm_start"}
            or value["version"] != VERSION
            or _canonical(value).decode() != context
        ):
            raise ValueError("Unsupported or noncanonical population context")
        return cls(value["roots"], horizon=value["horizon"], warm_start=value["warm_start"])

    @classmethod
    def from_legacy(cls, previous_game, *, source_iterations):
        if type(previous_game) is not MushroomContextGame or previous_game.num_players != 8:
            raise ValueError("Population warm start requires an eight-seat context game")
        return cls.default(horizon=previous_game.base_game.horizon).with_warm_start(
            previous_game, source_iterations=source_iterations
        )

    def with_warm_start(self, previous_game, *, source_iterations):
        if type(previous_game) is not MushroomContextGame:
            raise ValueError("Population warm start requires a fixed mushroom context game")
        if self.warm_start is not None:
            raise ValueError("Population already has warm-start provenance")
        game = type(self)(
            self.roots,
            horizon=self.horizon,
            warm_start={
                "previous_context": previous_game.checkpoint_context,
                "source_iterations": source_iterations,
                "schedule": "continue_total_iterations",
            },
        )
        game.validate_warm_start(previous_game, source_iterations=source_iterations)
        return game

    def validate_warm_start(self, previous_game, *, source_iterations):
        if (
            not isinstance(self.checkpoint_context, str)
            or sha256(self.checkpoint_context.encode()).hexdigest() != self._identity
            or type(previous_game) is not MushroomContextGame
            or previous_game.num_players != self.num_players
            or previous_game.rules_version != self.rules_version
            or previous_game.final_betting_round != self.final_betting_round
            or type(source_iterations) is not int
            or source_iterations < 0
            or self.warm_start
            != {
                "previous_context": previous_game.checkpoint_context,
                "source_iterations": source_iterations,
                "schedule": "continue_total_iterations",
            }
        ):
            raise ValueError("Population warm-start provenance does not match the source trainer")

    def validate_checkpoint_iterations(self, iterations):
        if (
            not isinstance(self.checkpoint_context, str)
            or sha256(self.checkpoint_context.encode()).hexdigest() != self._identity
            or type(iterations) is not int
            or iterations < 0
            or (self.warm_start is not None and iterations < self.warm_start["source_iterations"])
        ):
            raise ValueError("Checkpoint iterations precede the declared population warm start")

    def _game(self, index):
        if index not in self._games:
            root = self._roots[index]
            self._games[index] = MushroomContextGame(
                MushroomSessionGame(
                    root["stacks"],
                    button=root["button"],
                    horizon=self.horizon,
                    outstanding=root["outstanding"],
                    waiting_for_top_up=root["waiting_for_top_up"],
                )
            )
            if len(self._games) > 8:
                self._games.popitem(last=False)
        self._games.move_to_end(index)
        return self._games[index]

    def _check(self, state):
        if type(state) is not PopulationState or state.population != self._identity:
            raise ValueError("Population state belongs to a different training context")
        if state.root is None:
            if state.underlying is not None:
                raise ValueError("Unselected population root cannot contain a hand")
            return None
        if type(state.root) is not int or not 0 <= state.root < len(self._roots):
            raise ValueError("Invalid population root")
        if type(state.underlying) is not MushroomContextState:
            raise ValueError("Selected population requires a context state")
        return self._game(state.root)

    def initial_state(self):
        return PopulationState(self._identity)

    def actor(self, state):
        game = self._check(state)
        return -1 if game is None else game.actor(state.underlying)

    def active_players(self, state):
        game = self._check(state)
        return tuple(range(8)) if game is None else game.active_players(state.underlying)

    def betting_round(self, state):
        game = self._check(state)
        return 0 if game is None else game.betting_round(state.underlying)

    def chance_outcomes(self, state):
        game = self._check(state)
        return (
            tuple(
                (f"root:{i}", r["weight"] / self._total_weight) for i, r in enumerate(self._roots)
            )
            if game is None
            else game.chance_outcomes(state.underlying)
        )

    def sample_chance_outcome(self, state, rng):
        game = self._check(state)
        if game is not None:
            return game.sample_chance_outcome(state.underlying, rng)
        if len(self._roots) == 1:
            return "root:0", 1.0
        index = bisect_right(self._cumulative, rng.randrange(self._total_weight))
        return f"root:{index}", self._roots[index]["weight"] / self._total_weight

    def transition(self, state, action):
        game = self._check(state)
        if game is not None:
            return PopulationState(
                self._identity, state.root, game.transition(state.underlying, action)
            )
        if not isinstance(action, str) or action not in self.legal_actions(state):
            raise ValueError("An explicit population root action is required")
        index = int(action[5:])
        return PopulationState(self._identity, index, self._game(index).initial_state())

    def legal_actions(self, state):
        game = self._check(state)
        return (
            tuple(f"root:{i}" for i in range(len(self._roots)))
            if game is None
            else game.legal_actions(state.underlying)
        )

    def action_map(self, state):
        game = self._check(state)
        return {} if game is None else game.action_map(state.underlying)

    def concrete_action(self, state, action):
        return self.action_map(state).get(action, action)

    def information_key(self, state, player):
        game = self._check(state)
        if game is None:
            raise ValueError("No player information set before selecting the opening")
        return game.information_key(state.underlying, player)

    def search_information_key(self, state, player):
        game = self._check(state)
        if game is None:
            raise ValueError("No search information set before selecting the opening")
        return game.search_information_key(state.underlying, player)

    def public_key(self, state):
        game = self._check(state)
        return (
            self._identity,
            state.root,
            None if game is None else game.public_key(state.underlying),
        )

    def terminal_payoffs(self, state):
        game = self._check(state)
        if game is None:
            raise ValueError("Unselected population root is not terminal")
        return game.terminal_payoffs(state.underlying)

    def observation(self, state, player):
        game = self._check(state)
        if game is None:
            raise ValueError("Select the public opening before observing a hand")
        return game.observation(state.underlying, player)
