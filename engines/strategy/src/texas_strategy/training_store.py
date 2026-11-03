"""Bounded-cache, transactional Pluribus tables and portable SQLite snapshots.

Returned entries are copies; write them explicitly inside begin/commit.
Iteration updates spill to a private temporary database. JSON migration is a bounded
legacy import; existing databases never fall back to an older checkpoint.
"""

import json
import os
import sqlite3
import tempfile
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from math import isfinite
from pathlib import Path
from random import Random
from typing import Any

from texas_strategy.blueprint import (
    DEFAULT_MAX_BYTES,
    _canonical,
    _fields,
    _metadata,
    _nonfinite,
    _object,
    _trainer_metadata,
    load_checkpoint,
)
from texas_strategy.cfr import Trainer, _TableEntry
from texas_strategy.policy import InformationSet, PolicyEntry

SCHEMA_VERSION = 1
_PAGE_SIZE = 256
_WORKING_CACHE_SIZE = 4096
_COLUMNS = "id,player,information_key,actions,regrets,strategy_sum,stage,has_average"
_SCHEMA = (
    "CREATE TABLE training_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
    "document TEXT NOT NULL)",
    "CREATE TABLE training_entries (id INTEGER PRIMARY KEY CHECK(id>0), player INTEGER NOT NULL, "
    "information_key TEXT NOT NULL, actions TEXT NOT NULL, regrets TEXT NOT NULL, "
    "strategy_sum TEXT NOT NULL, stage INTEGER NOT NULL, "
    "has_average INTEGER NOT NULL CHECK(has_average IN (0,1)), "
    "UNIQUE(player,information_key))",
)


def _json(value: str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_object, parse_constant=_nonfinite)
    except (TypeError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid training database JSON") from exc


def _rng(state: Any) -> Random:
    if (
        not isinstance(state, list)
        or len(state) != 3
        or type(state[0]) is not int
        or state[0] != 3
        or not isinstance(state[1], list)
        or len(state[1]) != 625
        or any(type(v) is not int or not 0 <= v < 2**32 for v in state[1][:-1])
        or type(state[1][-1]) is not int
        or not 0 <= state[1][-1] <= 624
        or (state[2] is not None and (type(state[2]) not in (int, float) or not isfinite(state[2])))
    ):
        raise ValueError("Invalid training database RNG state")
    result = Random()
    result.setstate((state[0], tuple(state[1]), state[2]))
    return result


def _document(trainer: Trainer, iteration: int) -> dict[str, Any]:
    metadata = _trainer_metadata(trainer)
    metadata["iterations"] = iteration
    document = _json(
        _canonical(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "pluribus-training-store",
                "metadata": metadata,
                "rng_state": trainer._rng.getstate(),
                "training_context": getattr(trainer.game, "checkpoint_context", None),
            }
        )
    )
    _rng(document["rng_state"])
    return document


def _header(db: sqlite3.Connection, *, full_check: bool = True) -> dict[str, Any]:
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError("Unsupported training database schema version")
    if full_check and db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("Corrupt training database")
    objects = db.execute(
        "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    if objects != [("table", "training_entries"), ("table", "training_meta")]:
        raise ValueError("Invalid training database schema")
    definitions = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    if definitions != [(_SCHEMA[1],), (_SCHEMA[0],)]:
        raise ValueError("Invalid training database table constraints")
    if full_check and db.execute("SELECT 1 FROM training_entries WHERE id<=0 LIMIT 1").fetchone():
        raise ValueError("Invalid training database row identifier")
    rows = db.execute("SELECT singleton,document FROM training_meta").fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise ValueError("Missing training database metadata")
    document = _json(rows[0][1])
    _fields(
        document,
        {"schema_version", "kind", "metadata", "rng_state", "training_context"},
        "training database",
    )
    if (
        type(document["schema_version"]) is not int
        or document["schema_version"] != SCHEMA_VERSION
        or document["kind"] != "pluribus-training-store"
    ):
        raise ValueError("Unsupported training database format")
    metadata = _metadata(document["metadata"])
    if metadata["algorithm"] != "pluribus":
        raise ValueError("Persistent training requires Pluribus")
    _rng(document["rng_state"])
    return document


def _record(key: InformationSet, entry: _TableEntry, stage: int, game: Any) -> tuple:
    if (
        not isinstance(key, tuple)
        or len(key) != 2
        or type(key[0]) is not int
        or not 0 <= key[0] < game.num_players
        or not isinstance(key[1], str)
        or not key[1]
    ):
        raise ValueError("Invalid training information set")
    if type(stage) is not int or not 0 <= stage <= game.final_betting_round:
        raise ValueError("Invalid training betting round")
    if not isinstance(entry, _TableEntry) or not entry.actions:
        raise ValueError("Invalid training entry")
    PolicyEntry(entry.actions, (1 / len(entry.actions),) * len(entry.actions))
    for values in (entry.regrets, entry.strategy_sum):
        if (
            not isinstance(values, list)
            or len(values) != len(entry.actions)
            or any(type(v) not in (int, float) or not isfinite(v) for v in values)
        ):
            raise ValueError("Invalid training entry values")
    if any(v < 0 for v in entry.strategy_sum):
        raise ValueError("Strategy sums must be nonnegative")
    return (
        *key,
        _canonical(entry.actions).decode(),
        _canonical(entry.regrets).decode(),
        _canonical(entry.strategy_sum).decode(),
        stage,
        int(any(entry.strategy_sum)),
    )


def _insert(db: sqlite3.Connection, record: tuple) -> None:
    cursor = db.execute(
        "INSERT INTO training_entries "
        "(player,information_key,actions,regrets,strategy_sum,stage,has_average) "
        "VALUES (?,?,?,?,?,?,?) ON CONFLICT(player,information_key) DO UPDATE SET "
        "regrets=excluded.regrets,strategy_sum=excluded.strategy_sum,"
        "has_average=excluded.has_average WHERE training_entries.actions=excluded.actions "
        "AND training_entries.stage=excluded.stage",
        record,
    )
    if cursor.rowcount != 1:
        raise ValueError("Information set actions or betting round changed")


def _temporary(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    os.close(descriptor)
    return Path(name)


def _publish(temporary: Path, target: Path) -> None:
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    # Hard-link publication is atomic and never replaces a concurrent/live database.
    os.link(temporary, target)
    descriptor = os.open(target.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup_checkpoint(source: str | Path, target: str | Path) -> None:
    """Atomically publish a consistent snapshot; the output must not already exist."""
    source, target = Path(source).absolute(), Path(target).absolute()
    if source.resolve() == target.resolve():
        raise ValueError("Backup destination must differ from the source")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Backup destination already exists: {target}")
    temporary = _temporary(target)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA cache_size=-8192")
            db.execute("PRAGMA mmap_size=0")
            db.execute("PRAGMA temp_store=FILE")
            _header(db)
            with closing(sqlite3.connect(temporary)) as destination:
                db.backup(destination, pages=256)
                destination.execute("PRAGMA journal_mode=DELETE")
        _publish(temporary, target)
    except sqlite3.Error as exc:
        raise ValueError(f"Invalid or unavailable training database: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


class _Stages(Mapping[InformationSet, int]):
    def __init__(self, store: "TrainingStore") -> None:
        self._store = store

    def __len__(self) -> int:
        return len(self._store)

    def __iter__(self) -> Iterator[InformationSet]:
        return iter(self._store)

    def __getitem__(self, key: InformationSet) -> int:
        row = self._store._db.execute(
            "SELECT stage FROM training_entries WHERE player=? AND information_key=?", key
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return row[0]


class _IterationStore:
    """Keep active recursion entries identical; spill inactive entries within one iteration."""

    def __init__(self, store: "TrainingStore") -> None:
        self._store = store
        self._cache = OrderedDict()
        self._active = {}
        self._db = sqlite3.connect("")
        try:
            self._db.execute("PRAGMA cache_size=-8192")
            self._db.execute("PRAGMA mmap_size=0")
            self._db.execute("PRAGMA temp_store=FILE")
            # This scratch database is discarded on any failure; the main store stays FULL/WAL.
            self._db.execute("PRAGMA journal_mode=OFF")
            self._db.execute("PRAGMA synchronous=OFF")
            self._db.execute(_SCHEMA[1])
        except BaseException:
            self._db.close()
            raise

    def _save(self, key, entry, stage):
        _insert(self._db, _record(key, entry, stage, self._store.game))

    @contextmanager
    def entry(self, key, actions, stage):
        if key not in self._active:
            cached = self._cache.pop(key, None)
            if cached is None:
                row = self._db.execute(
                    f"SELECT {_COLUMNS} FROM training_entries WHERE player=? AND information_key=?",
                    key,
                ).fetchone()
                if row is not None:
                    _, entry, previous_stage = self._store._decode(row)
                else:
                    try:
                        entry = self._store[key]
                        previous_stage = _Stages(self._store)[key]
                    except KeyError:
                        entry = _TableEntry(actions, [0.0] * len(actions), [0.0] * len(actions))
                        previous_stage = stage
                cached = entry, previous_stage
            self._active[key] = [*cached, 0]
        active = self._active[key]
        entry, previous_stage, _ = active
        if entry.actions != actions or previous_stage != stage:
            raise ValueError("Information set changes legal actions or betting round")
        active[2] += 1
        try:
            yield entry
        finally:
            active[2] -= 1
            if not active[2]:
                del self._active[key]
                self._cache[key] = entry, stage
                while len(self._cache) > _WORKING_CACHE_SIZE:
                    old_key, (old_entry, old_stage) = self._cache.popitem(last=False)
                    self._save(old_key, old_entry, old_stage)

    def __contains__(self, key):
        return (
            key in self._active
            or key in self._cache
            or self._db.execute(
                "SELECT 1 FROM training_entries WHERE player=? AND information_key=?", key
            ).fetchone()
            is not None
        )

    def scan(self):
        if self._active:
            raise ValueError("Cannot persist an active traversal")
        while self._cache:
            key, (entry, stage) = self._cache.popitem(last=False)
            self._save(key, entry, stage)
        for row in self._db.execute(f"SELECT {_COLUMNS} FROM training_entries ORDER BY id"):
            yield self._store._decode(row)

    def close(self):
        self._cache.clear()
        self._active.clear()
        self._db.close()


class TrainingStore(Mapping[InformationSet, _TableEntry]):
    def __init__(self, game: Any, path: str | Path) -> None:
        self.path = Path(path).absolute()
        self.game = game
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
        try:
            self._db.execute("PRAGMA cache_size=-8192")
            self._db.execute("PRAGMA mmap_size=0")
            self._db.execute("PRAGMA temp_store=FILE")
            self._db.execute("BEGIN")
            document = _header(self._db)
            metadata = _metadata(document["metadata"], game)
            if document["training_context"] != getattr(game, "checkpoint_context", None):
                raise ValueError("Training context does not match the starting game")
            self._db.execute("COMMIT")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            # Changing journal mode changes data_version too; bind counts only afterwards.
            self._db.execute("BEGIN")
            if _header(self._db, full_check=False) != document:
                raise ValueError("Stale training database while opening; reopen the database")
            self._data_version = self._db.execute("PRAGMA data_version").fetchone()[0]
            self._metadata = metadata
            self._iterations = metadata["iterations"]
            self._document = document
            self._counts = self._scan_counts()
            self._counted_changes = self._db.total_changes
            self._rollback_counts = None
            if self._iterations == 0 and self._counts[0]:
                raise ValueError("Untrained database cannot contain accumulated information sets")
            self._db.execute("COMMIT")
            self._check_counts()
        except BaseException:
            self._db.close()
            raise

    def _decode(self, row: tuple) -> tuple[InformationSet, _TableEntry, int]:
        _, player, key, actions, regrets, sums, stage, average = row
        actions = _json(actions)
        if not isinstance(actions, list):
            raise ValueError("Training actions must be an array")
        entry = _TableEntry(tuple(actions), _json(regrets), _json(sums))
        record = _record((player, key), entry, stage, self.game)
        if type(average) is not int or average != record[-1]:
            raise ValueError("Invalid training average count")
        return (player, key), entry, stage

    def _rows(self, columns: str, *, key_prefix: str = "") -> Iterator[tuple]:
        ceiling = self._db.execute("SELECT COALESCE(MAX(id),0) FROM training_entries").fetchone()[0]
        after = 0
        while True:
            rows = self._db.execute(
                f"SELECT {columns} FROM training_entries WHERE id>? AND id<=? "
                + ("AND substr(information_key,1,length(?))=? " if key_prefix else "")
                + "ORDER BY id LIMIT ?",
                (after, ceiling, *((key_prefix, key_prefix) if key_prefix else ()), _PAGE_SIZE),
            ).fetchall()
            if not rows:
                return
            after = rows[-1][0]
            yield from rows

    def __len__(self) -> int:
        return self.counts()[0]

    def __iter__(self) -> Iterator[InformationSet]:
        for _, player, key in self._rows("id,player,information_key"):
            yield player, key

    def __getitem__(self, key: InformationSet) -> _TableEntry:
        row = self._db.execute(
            f"SELECT {_COLUMNS} FROM training_entries WHERE player=? AND information_key=?", key
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return self._decode(row)[1]

    def items(self) -> Iterator[tuple[InformationSet, _TableEntry]]:
        for key, entry, _stage in self.scan():
            yield key, entry

    def values(self) -> Iterator[_TableEntry]:
        for _key, entry, _stage in self.scan():
            yield entry

    def scan(self, *, key_prefix: str = "") -> Iterator[tuple[InformationSet, _TableEntry, int]]:
        """Stream original row IDs once; updates keep IDs, later inserts are excluded."""
        for row in self._rows(_COLUMNS, key_prefix=key_prefix):
            yield self._decode(row)

    def counts(self) -> tuple[int, int]:
        """Return this writer's transaction-aligned counts, never a stale external snapshot."""
        if self._db.in_transaction != (self._rollback_counts is not None):
            raise ValueError("Stale training counts after an untracked transaction; reopen")
        self._check_counts()
        return self._counts

    def _check_counted_changes(self) -> None:
        if self._db.total_changes != self._counted_changes:
            raise ValueError("Stale training counts after an untracked write; reopen the database")

    def _scan_counts(self) -> tuple[int, int]:
        total = averaged = 0
        for _key, entry, _stage in self.scan():
            total += 1
            averaged += int(any(entry.strategy_sum))
        return total, averaged

    def _check_counts(self) -> None:
        self._check_counted_changes()
        if self._db.execute("PRAGMA data_version").fetchone()[0] != self._data_version:
            # A WAL truncation can change data_version without changing any logical row.
            self._refresh_counts()

    def _refresh_counts(self) -> None:
        own_transaction = not self._db.in_transaction
        if own_transaction:
            self._db.execute("BEGIN")
        try:
            if _header(self._db, full_check=False) != self._document:
                raise ValueError("Stale training writer; reopen the database")
            version = self._db.execute("PRAGMA data_version").fetchone()[0]
            if self._db.execute("SELECT 1 FROM training_entries WHERE id<=0 LIMIT 1").fetchone():
                raise ValueError("Invalid training database row identifier")
            counts = self._scan_counts()
            if self._iterations == 0 and counts[0]:
                raise ValueError("Untrained database cannot contain accumulated information sets")
            if own_transaction:
                self._db.execute("COMMIT")
            if self._db.execute("PRAGMA data_version").fetchone()[0] != version:
                raise ValueError("Stale training database during count refresh; reopen")
            self._counts, self._data_version = counts, version
        except BaseException:
            if own_transaction and self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def begin(self, expected_iteration: int | None = None) -> None:
        if self._rollback_counts is not None and not self._db.in_transaction:
            raise ValueError("Stale training transaction; reopen the database")
        if self._db.in_transaction:
            raise ValueError("Training transaction already active")
        expected = self._iterations if expected_iteration is None else expected_iteration
        if type(expected) is not int or expected != self._iterations:
            raise ValueError("Stale training iteration")
        self._check_counted_changes()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            document = _json(
                self._db.execute("SELECT document FROM training_meta WHERE singleton=1").fetchone()[
                    0
                ]
            )
            if document != self._document:
                raise ValueError("Stale training writer; reopen the database")
            self._check_counts()
            self._rollback_counts = self._counts
        except BaseException:
            self.rollback()
            raise

    def set_entry(self, key: InformationSet, entry: _TableEntry, stage: int) -> None:
        if not self._db.in_transaction or self._rollback_counts is None:
            raise ValueError("Entry updates require an active training transaction")
        record = _record(key, entry, stage, self.game)
        if any(entry.regrets) or any(entry.strategy_sum):
            self._check_counted_changes()
            previous = self._db.execute(
                "SELECT has_average FROM training_entries WHERE player=? AND information_key=?",
                key,
            ).fetchone()
            _insert(self._db, record)
            self._counted_changes += 1
            self._check_counted_changes()
            self._counts = (
                self._counts[0] + int(previous is None),
                self._counts[1] + record[-1] - (previous[0] if previous else 0),
            )

    def commit(self, trainer: Trainer, next_iteration: int) -> None:
        if not self._db.in_transaction or self._rollback_counts is None:
            raise ValueError("No active training transaction")
        if (
            type(next_iteration) is not int
            or next_iteration != self._iterations + 1
            or trainer.iterations != self._iterations
        ):
            raise ValueError("Invalid training iteration commit")
        document = _document(trainer, next_iteration)
        prior = {
            **document,
            "metadata": {**document["metadata"], "iterations": self._iterations},
            "rng_state": self._document["rng_state"],
        }
        if prior != self._document:
            raise ValueError("Training metadata changed during the iteration")
        self._check_counts()
        self._db.execute(
            "UPDATE training_meta SET document=? WHERE singleton=1",
            (_canonical(document).decode(),),
        )
        self._counted_changes += 1
        self._check_counted_changes()
        self._db.execute("COMMIT")
        self._rollback_counts = None
        self._iterations = next_iteration
        self._document = document

    def rollback(self) -> None:
        if self._db.in_transaction:
            self._db.execute("ROLLBACK")
            if self._rollback_counts is not None:
                self._counts = self._rollback_counts
                self._rollback_counts = None
                self._counted_changes = self._db.total_changes

    def enable_sampled_average(self, trainer: Trainer, *, samples: int = 16) -> None:
        """Explicitly start preflop averaging without discarding existing training.

        Old nonzero preflop sums cannot be reweighted without their sampling history.
        Callers must stop the worker and back up its committed database first.
        """
        from texas_strategy.pluribus import PluribusConfig

        if getattr(trainer, "_store", None) is not self:
            raise ValueError("Trainer does not own this training store")
        previous = trainer.pluribus
        if previous.average_mode != "enumerated":
            raise ValueError("Sampled averaging is already configured")
        updated = PluribusConfig.from_dict(
            {
                **previous.to_dict(),
                "strategy_interval": 1,
                "average_mode": "sampled_uniform",
                "average_samples": samples,
                "sampled_average_from": trainer.iterations + 1,
            }
        )
        self.begin(trainer.iterations)
        try:
            if self._db.execute(
                "SELECT 1 FROM training_entries WHERE stage=0 AND has_average=1 LIMIT 1"
            ).fetchone():
                raise ValueError("Existing preflop averages cannot be silently reweighted")
            trainer.pluribus = updated
            document = _document(trainer, trainer.iterations)
            self._db.execute(
                "UPDATE training_meta SET document=? WHERE singleton=1",
                (_canonical(document).decode(),),
            )
            self._counted_changes += 1
            self._check_counted_changes()
            self._db.execute("COMMIT")
            self._rollback_counts = None
        except BaseException:
            trainer.pluribus = previous
            self.rollback()
            raise
        self._document = document
        self._metadata = document["metadata"]

    def enable_mixed_average(self, trainer: Trainer) -> None:
        """Change only the sampling proposal, after the caller backs up committed progress.

        Both estimators target the same counts; existing nonzero averages and
        their original start iteration remain intact. The switch is recorded.
        """
        from texas_strategy.pluribus import PluribusConfig

        if getattr(trainer, "_store", None) is not self or trainer.game is not self.game:
            raise ValueError("Trainer does not own this training store and game")
        previous = trainer.pluribus
        if previous.average_mode != "sampled_uniform":
            raise ValueError("Mixture upgrade requires the existing uniform sampled average")
        if _document(trainer, trainer.iterations) != self._document:
            raise ValueError("Trainer differs from the committed averaging source")
        updated = PluribusConfig.from_dict(
            {
                **previous.to_dict(),
                "average_mode": "sampled_mixture",
                "mixture_from": trainer.iterations + 1,
            }
        )
        self.begin(trainer.iterations)
        try:
            trainer.pluribus = updated
            document = _document(trainer, trainer.iterations)
            self._db.execute(
                "UPDATE training_meta SET document=? WHERE singleton=1",
                (_canonical(document).decode(),),
            )
            self._counted_changes += 1
            self._check_counted_changes()
            self._db.execute("COMMIT")
            self._rollback_counts = None
        except BaseException:
            trainer.pluribus = previous
            self.rollback()
            raise
        self._document = document
        self._metadata = document["metadata"]

    def enable_population_training(self, trainer: Trainer, population_game: Any) -> None:
        """Retain cumulative values while explicitly changing the public root distribution.

        Call between committed iterations; back up before the first migration. The
        target manifest freezes the source iteration and keeps the total-iteration
        schedule. Retained sums are initialization, not samples from the new root
        distribution; no accumulated value or RNG state is reweighted or reset.
        """
        from texas_strategy.mushroom_population import MushroomPopulationGame

        if getattr(trainer, "_store", None) is not self or trainer.game is not self.game:
            raise ValueError("Trainer does not own this training store and game")
        if not isinstance(population_game, MushroomPopulationGame):
            raise ValueError("Expected a validated mushroom population game")
        previous = trainer.game
        if isinstance(previous, MushroomPopulationGame):
            population_game.validate_opening_update(previous, source_iterations=trainer.iterations)
        else:
            population_game.validate_warm_start(previous, source_iterations=trainer.iterations)
        if population_game.final_betting_round != previous.final_betting_round:
            raise ValueError("Population training cannot change betting-round semantics")
        if _document(trainer, trainer.iterations) != self._document:
            raise ValueError("Trainer differs from the committed warm-start source")
        self.begin(trainer.iterations)
        try:
            trainer.game = self.game = population_game
            document = _document(trainer, trainer.iterations)
            if {
                **document,
                "training_context": self._document["training_context"],
            } != self._document:
                raise ValueError("Population training cannot change model metadata or RNG")
            self._db.execute(
                "UPDATE training_meta SET document=? WHERE singleton=1",
                (_canonical(document).decode(),),
            )
            self._counted_changes += 1
            self._check_counted_changes()
            self._db.execute("COMMIT")
            self._rollback_counts = None
        except BaseException:
            trainer.game = self.game = previous
            self.rollback()
            raise
        self._document = document
        self._metadata = document["metadata"]

    def enable_ante_training(self, trainer: Trainer, population_game: Any) -> None:
        """Keep legacy rows/RNG, but isolate new ante keys and record their start iteration.

        Back up the committed database first. Old rows remain portable history;
        their presence is not coverage or feedback for the new rules.
        """
        from texas_strategy.mushroom_population import MushroomPopulationGame

        if getattr(trainer, "_store", None) is not self or trainer.game is not self.game:
            raise ValueError("Trainer does not own this training store and game")
        if not isinstance(population_game, MushroomPopulationGame):
            raise ValueError("Expected a validated mushroom population game")
        previous = trainer.game
        population_game.validate_rule_migration(previous, source_iterations=trainer.iterations)
        if _document(trainer, trainer.iterations) != self._document:
            raise ValueError("Trainer differs from the committed migration source")
        self.begin(trainer.iterations)
        try:
            if any(
                self._db.execute(
                    "SELECT 1 FROM training_entries WHERE player=? AND information_key>=? "
                    "AND information_key<? LIMIT 1",
                    (player, "ante4:", "ante4;"),
                ).fetchone()
                for player in range(population_game.num_players)
            ):
                raise ValueError("Legacy store already contains the new ante namespace")
            trainer.game = self.game = population_game
            document = _document(trainer, trainer.iterations)
            expected = {
                **self._document,
                "training_context": population_game.checkpoint_context,
                "metadata": {
                    **self._document["metadata"],
                    "rules_version": population_game.rules_version,
                },
            }
            if document != expected:
                raise ValueError("Ante migration cannot change accumulated iterations or RNG")
            self._db.execute(
                "UPDATE training_meta SET document=? WHERE singleton=1",
                (_canonical(document).decode(),),
            )
            self._counted_changes += 1
            self._check_counted_changes()
            self._db.execute("COMMIT")
            self._rollback_counts = None
        except BaseException:
            trainer.game = self.game = previous
            self.rollback()
            raise
        self._document = document
        self._metadata = document["metadata"]

    def close(self) -> None:
        self._db.close()

    def bytes_on_disk(self) -> int:
        return sum(
            p.stat().st_size
            for p in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"))
            if p.exists()
        )

    def backup(self, target: str | Path) -> None:
        if self._db.in_transaction:
            raise ValueError("Back up only committed training iterations")
        backup_checkpoint(self.path, target)


def open_trainer(
    game: Any,
    path: str | Path,
    *,
    checkpoint: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Trainer:
    """Open durable progress, or atomically migrate an existing legacy JSON checkpoint."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    target = Path(path).absolute()
    if not target.exists() and not target.is_symlink():
        trainer = (
            load_checkpoint(game, checkpoint, max_bytes=max_bytes)
            if checkpoint is not None
            and (Path(checkpoint).exists() or Path(checkpoint).is_symlink())
            else Trainer(game, algorithm="pluribus", seed=42)
        )
        if trainer.algorithm != "pluribus":
            raise ValueError("Persistent training requires Pluribus")
        temporary = _temporary(target)
        try:
            db = sqlite3.connect(temporary)
            try:
                for statement in _SCHEMA:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.execute(
                    "INSERT INTO training_meta VALUES (1,?)",
                    (_canonical(_document(trainer, trainer.iterations)).decode(),),
                )
                for key, entry in trainer._table.items():
                    _insert(db, _record(key, entry, trainer._betting_rounds[key], game))
                db.commit()
            finally:
                db.close()
            try:
                _publish(temporary, target)
            except FileExistsError:
                pass  # A concurrent initializer won; validate and resume its database.
        finally:
            temporary.unlink(missing_ok=True)
        del trainer  # Release the legacy in-memory table before validating the durable store.
    store = None
    try:
        store = TrainingStore(game, target)
        metadata = store._metadata
        trainer = Trainer(
            game,
            algorithm="pluribus",
            weighting=metadata["weighting"],
            seed=metadata["seed"],
            exploration=metadata["exploration"],
            pluribus=metadata["pluribus"],
        )
        trainer.iterations = metadata["iterations"]
        if _trainer_metadata(trainer)["chance_sampling"] != metadata["chance_sampling"]:
            raise ValueError("Training chance sampling does not match this game implementation")
        trainer._rng = _rng(store._document["rng_state"])
        trainer._store = store
        trainer._table = store
        trainer._betting_rounds = _Stages(store)
        return trainer
    except BaseException as exc:
        if store is not None:
            store.close()
        if isinstance(exc, sqlite3.Error):
            raise ValueError(f"Invalid or unavailable training database: {exc}") from exc
        raise
