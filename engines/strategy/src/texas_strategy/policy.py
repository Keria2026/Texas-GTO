"""Information-set policies; absent entries are explicit, observable fallbacks."""

from collections.abc import Mapping
from dataclasses import dataclass
from math import fsum, isclose, isfinite
from types import MappingProxyType
from typing import Any, Literal

InformationSet = tuple[int, str]


def legal_information(game: Any, state: Any) -> tuple[InformationSet, tuple[str, ...]]:
    actor = game.actor(state)
    if type(actor) is not int or not 0 <= actor < game.num_players:
        raise ValueError("A policy query requires a player decision node")
    key = game.information_key(state, actor)
    if not isinstance(key, str) or not key:
        raise ValueError("Information keys must be nonempty strings")
    actions = tuple(game.legal_actions(state))
    if not actions or any(not isinstance(action, str) or not action for action in actions):
        raise ValueError("Legal actions must be nonempty strings")
    if len(set(actions)) != len(actions):
        raise ValueError("Legal actions must be distinct")
    return (actor, key), actions


@dataclass(frozen=True)
class PolicyEntry:
    actions: tuple[str, ...]
    probabilities: tuple[float, ...]

    def __post_init__(self) -> None:
        actions = tuple(self.actions)
        probabilities = tuple(self.probabilities)
        if (
            not actions
            or any(not isinstance(action, str) or not action for action in actions)
            or len(set(actions)) != len(actions)
        ):
            raise ValueError("Policy actions must be distinct nonempty strings")
        if len(actions) != len(probabilities) or any(
            type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1
            for value in probabilities
        ):
            raise ValueError("Probabilities must be finite, nonnegative and action-aligned")
        if not isclose(fsum(probabilities), 1.0, rel_tol=0, abs_tol=1e-10):
            raise ValueError("Probabilities must sum to one")
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "probabilities", tuple(float(p) for p in probabilities))


class TabularPolicy:
    """A behavioral strategy with auditable coverage and legal-action alignment.

    Uniform fallback makes incomplete research trees usable for rollouts. It is
    never evidence that an information set was trained: callers can use
    ``contains``/``lookup`` or select ``missing='error'`` before recommending.
    Entries are immutable; query counters are diagnostic only.
    """

    def __init__(
        self,
        entries: Mapping[InformationSet, PolicyEntry] | None = None,
        *,
        missing: Literal["uniform", "error"] = "uniform",
    ) -> None:
        if missing not in ("uniform", "error"):
            raise ValueError("Unknown missing-policy behavior")
        copied = dict(entries or {})
        for key, entry in copied.items():
            if (
                not isinstance(key, tuple)
                or len(key) != 2
                or type(key[0]) is not int
                or key[0] < 0
                or not isinstance(key[1], str)
                or not key[1]
                or not isinstance(entry, PolicyEntry)
            ):
                raise ValueError("Invalid information-set policy entry")
        self.entries = MappingProxyType(copied)
        self.missing = missing
        self.fallback_count = 0
        self.covered_queries = 0

    def lookup(self, player: int, information_key: str) -> PolicyEntry | None:
        return self.entries.get((player, information_key))

    def contains(self, game: Any, state: Any) -> bool:
        key, actions = legal_information(game, state)
        entry = self.entries.get(key)
        if entry is not None and entry.actions != actions:
            raise ValueError("Policy actions do not match the game's legal action order")
        return entry is not None

    def probabilities(self, game: Any, state: Any) -> tuple[float, ...]:
        key, actions = legal_information(game, state)
        entry = self.entries.get(key)
        if entry is None:
            self.fallback_count += 1
            if self.missing == "error":
                raise KeyError(f"Policy does not cover information set {key!r}")
            return (1.0 / len(actions),) * len(actions)
        if entry.actions != actions:
            raise ValueError("Policy actions do not match the game's legal action order")
        self.covered_queries += 1
        return entry.probabilities
