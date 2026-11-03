"""Read one committed average-policy snapshot without loading the training table.

Keep the context short: it pins a WAL read transaction until exit. Metadata and
queries share that transaction; callers must bind results to their model source
and observed state. A header digest identifies metadata, not table integrity or
strategy quality. Starting stacks/button may differ when the rules explicitly
share an information encoding, as in MushroomContextGame.
"""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from math import fsum, isfinite
from pathlib import Path
from typing import Any, Literal

from texas_strategy.blueprint import _canonical, _metadata
from texas_strategy.policy import PolicyEntry, legal_information
from texas_strategy.training_store import _header, _json


def _binding(game: Any) -> tuple:
    return (
        game.game_id,
        game.rules_version,
        game.num_players,
        "game_exact" if callable(getattr(game, "sample_chance_outcome", None)) else "enumerated",
    )


class PersistentPolicy:
    """TabularPolicy query interface, valid only inside ``policy_snapshot``."""

    def __init__(self, db: sqlite3.Connection, game: Any, document: dict, missing: str):
        self._db: sqlite3.Connection | None = db
        self._metadata = _metadata(document["metadata"], game)
        self._binding = _binding(game)
        if self._metadata["chance_sampling"] != self._binding[-1]:
            raise ValueError("Policy chance sampling does not match the game implementation")
        self._final_round = game.final_betting_round
        self.metadata_digest = hashlib.sha256(_canonical(document)).hexdigest()
        self.missing = missing
        self.fallback_count = 0
        self.covered_queries = 0

    @property
    def metadata(self) -> dict:
        return deepcopy(self._metadata)

    @property
    def iterations(self) -> int:
        return self._metadata["iterations"]

    def lookup(self, player: int, information_key: str) -> PolicyEntry | None:
        if self._db is None:
            raise ValueError("Policy snapshot is closed")
        if (
            type(player) is not int
            or not 0 <= player < self._metadata["num_players"]
            or not isinstance(information_key, str)
            or not information_key
        ):
            raise ValueError("Invalid policy information set")
        row = self._db.execute(
            "SELECT actions,strategy_sum,stage,has_average FROM training_entries "
            "WHERE player=? AND information_key=?",
            (player, information_key),
        ).fetchone()
        if row is None:
            return None
        if self.iterations == 0:
            raise ValueError("Untrained database contains accumulated information sets")
        raw_actions, raw_sums, stage, has_average = row
        if type(stage) is not int or not 0 <= stage <= self._final_round:
            raise ValueError("Invalid policy betting round")
        actions, sums = _json(raw_actions), _json(raw_sums)
        if not isinstance(actions, list) or not actions:
            raise ValueError("Invalid policy actions")
        actions = tuple(actions)
        PolicyEntry(actions, (1 / len(actions),) * len(actions))
        try:
            valid = (
                isinstance(sums, list)
                and len(sums) == len(actions)
                and all(type(v) in (int, float) and isfinite(v) and v >= 0 for v in sums)
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("Invalid average strategy sums")
        maximum = max(sums)
        if type(has_average) is not int or has_average != int(maximum > 0):
            raise ValueError("Invalid average policy coverage marker")
        if maximum == 0:
            return None
        scaled = [value / maximum for value in sums]
        total = fsum(scaled)
        return PolicyEntry(actions, tuple(value / total for value in scaled))

    def is_uniform_without_regrets(self, player: int, information_key: str) -> bool:
        """Check current values, not visit counts or convergence.

        Equal values can also result from legitimate training; without visit
        provenance this conservative check cannot distinguish them from initialization.
        """
        entry = self.lookup(player, information_key)
        if entry is None:
            return True
        row = self._db.execute(
            "SELECT regrets FROM training_entries WHERE player=? AND information_key=?",
            (player, information_key),
        ).fetchone()
        regrets = _json(row[0])
        try:
            valid = (
                isinstance(regrets, list)
                and len(regrets) == len(entry.actions)
                and all(type(v) in (int, float) and isfinite(v) for v in regrets)
            )
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("Invalid policy regret evidence")
        return not any(regrets) and len(set(entry.probabilities)) == 1

    def _query(self, game: Any, state: Any) -> tuple:
        if _binding(game) != self._binding:
            raise ValueError("Policy query game/rules do not match this snapshot")
        key, actions = legal_information(game, state)
        entry = self.lookup(*key)
        if entry is not None and entry.actions != actions:
            raise ValueError("Policy actions do not match the game's legal action order")
        return key, actions, entry

    def contains(self, game: Any, state: Any) -> bool:
        return self._query(game, state)[2] is not None

    def probabilities(self, game: Any, state: Any) -> tuple[float, ...]:
        key, actions, entry = self._query(game, state)
        if entry is None:
            self.fallback_count += 1
            if self.missing == "error":
                raise KeyError(f"Policy does not cover information set {key!r}")
            return (1.0 / len(actions),) * len(actions)
        self.covered_queries += 1
        return entry.probabilities


@contextmanager
def policy_snapshot(
    game: Any,
    path: str | Path,
    *,
    missing: Literal["error", "uniform"] = "error",
) -> Iterator[PersistentPolicy]:
    """Read a fixed committed iteration; exceptions also release the WAL reader."""
    if missing not in ("error", "uniform"):
        raise ValueError("Unknown missing-policy behavior")
    db = None
    policy = None
    try:
        db = sqlite3.connect(
            Path(path).absolute().as_uri() + "?mode=ro",
            uri=True,
            isolation_level=None,
        )
        db.execute("PRAGMA cache_size=-8192")
        db.execute("PRAGMA mmap_size=0")
        db.execute("PRAGMA temp_store=FILE")
        db.execute("BEGIN")
        policy = PersistentPolicy(db, game, _header(db, full_check=False), missing)
        yield policy
    except sqlite3.Error as exc:
        raise ValueError(f"Invalid or unavailable policy database: {exc}") from exc
    finally:
        if policy is not None:
            policy._db = None
        if db is not None:
            db.close()
