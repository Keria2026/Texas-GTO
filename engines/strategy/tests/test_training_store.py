"""Durable table recovery, bounded scans, immutable migrations and atomic snapshots."""

import hashlib
import json
import sqlite3
import tracemalloc
from collections.abc import Mapping
from contextlib import closing

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.cfr import Trainer, _TableEntry
from texas_strategy.training_store import TrainingStore, backup_checkpoint, open_trainer


def entry(value=1):
    return _TableEntry(("check", "bet"), [-value, value], [value, 0])


def commit(trainer, values=None):
    trainer._store.begin(trainer.iterations)
    for key, value in (values or {(0, "first"): entry()}).items():
        trainer._store.set_entry(key, value, 0)
    trainer._rng.random()
    trainer._store.commit(trainer, trainer.iterations + 1)
    trainer.iterations += 1


def rewrite_document(path, update):
    with closing(sqlite3.connect(path)) as db, db:
        document = json.loads(db.execute("SELECT document FROM training_meta").fetchone()[0])
        update(document)
        db.execute("UPDATE training_meta SET document=?", (json.dumps(document),))


def test_fresh_store_is_bounded_readonly_and_resumes_rng(tmp_path):
    path = tmp_path / "table.sqlite3"
    trainer = open_trainer(KuhnPoker(), path, checkpoint=tmp_path / "missing.json")
    store = trainer._store
    assert trainer.algorithm == "pluribus" and trainer.seed == 42
    assert isinstance(store, TrainingStore) and isinstance(store, Mapping)
    assert store.counts() == (0, 0)
    assert store._db.execute("PRAGMA cache_size").fetchone()[0] == -8192
    assert store._db.execute("PRAGMA mmap_size").fetchone()[0] == 0
    assert store._db.execute("PRAGMA temp_store").fetchone()[0] == 1
    with pytest.raises(ValueError, match="transaction"):
        store.set_entry((0, "first"), entry(), 0)
    commit(trainer)
    saved_rng = trainer._rng.getstate()
    assert list(store) == [(0, "first")]
    assert list(store.items()) == [((0, "first"), entry())]
    assert list(store.values()) == [entry()]
    assert dict(trainer._betting_rounds) == {(0, "first"): 0}
    with pytest.raises(TypeError):
        store[(0, "first")] = entry(2)
    with pytest.raises(TypeError):
        trainer._betting_rounds[(0, "first")] = 1
    store[(0, "first")].regrets[0] = 123
    assert store[(0, "first")].regrets[0] == -1
    assert store.counts() == (1, 1) and store.bytes_on_disk() >= path.stat().st_size
    store.close()
    restored = open_trainer(KuhnPoker(), path)
    try:
        assert restored.iterations == 1
        assert restored._rng.getstate() == saved_rng
        assert dict(restored._table) == {(0, "first"): entry()}
    finally:
        restored._store.close()


def test_dangling_legacy_link_does_not_create_fresh_training(tmp_path):
    legacy = tmp_path / "checkpoint.json"
    legacy.symlink_to(tmp_path / "missing-import.json")
    database = tmp_path / "training.sqlite3"
    with pytest.raises(FileNotFoundError):
        open_trainer(KuhnPoker(), database, checkpoint=legacy)
    assert legacy.is_symlink()
    assert not database.exists()


def test_rollback_preserves_rows_metadata_and_rejects_stale_writer(tmp_path):
    path = tmp_path / "table.sqlite3"
    first = open_trainer(KuhnPoker(), path)
    stale = open_trainer(KuhnPoker(), path)
    commit(first)
    with pytest.raises(ValueError, match="Stale"):
        stale._store.begin(0)
    assert not stale._store._db.in_transaction
    first._store.begin(1)
    first._store.set_entry((0, "first"), entry(8), 0)
    first._store.set_entry((1, "discarded"), entry(9), 0)
    with pytest.raises(ValueError, match="iteration"):
        first._store.commit(first, 3)
    first._store.rollback()
    assert first._store.counts() == (1, 1)
    assert first._table[(0, "first")] == entry()
    first._store.close()
    stale._store.close()
    restored = open_trainer(KuhnPoker(), path)
    assert restored.iterations == 1
    restored._store.close()


def test_scan_visits_original_rows_once_while_updating_and_inserting(tmp_path):
    trainer = open_trainer(KuhnPoker(), tmp_path / "scan.sqlite3")
    store = trainer._store
    commit(trainer, {(i % 2, str(i)): entry(i + 1) for i in range(800)})
    store.begin(1)
    seen = set()
    for key, value, stage in store.scan():
        assert key not in seen
        seen.add(key)
        value.regrets[0] *= 0.5
        store.set_entry(key, value, stage)
        store.set_entry((key[0], "new-" + key[1]), entry(4), 0)
    assert len(seen) == 800
    assert store.counts() == (1600, 1600)
    store.commit(trainer, 2)
    trainer.iterations = 2
    assert store[(0, "0")].regrets == [-0.5, 1]
    store.close()


def test_zero_updates_preserve_existing_rows_and_average_counts(tmp_path):
    trainer = open_trainer(KuhnPoker(), tmp_path / "zero.sqlite3")
    store = trainer._store
    commit(trainer)
    store.begin(1)
    zero = _TableEntry(("check", "bet"), [0, 0], [0, 0])
    store.set_entry((0, "new"), zero, 0)
    store.set_entry((0, "first"), zero, 0)
    assert store.counts() == (1, 1)
    store.set_entry((0, "first"), _TableEntry(("check", "bet"), [1, 0], [0, 0]), 0)
    assert store.counts() == (1, 0)
    store.rollback()
    assert store.counts() == (1, 1)
    store.close()


def test_legacy_migration_preserves_all_progress_and_existing_db_wins(tmp_path):
    game = KuhnPoker()
    original = Trainer(game, algorithm="pluribus", seed=17)
    original.train(20)
    legacy = tmp_path / "old.json"
    original.save_checkpoint(legacy)
    digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
    path = tmp_path / "durable.sqlite3"
    restored = open_trainer(game, path, checkpoint=legacy)
    assert restored.iterations == 20
    assert dict(restored._table) == original._table
    assert dict(restored._betting_rounds) == original._betting_rounds
    assert restored._rng.getstate() == original._rng.getstate()
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == digest
    restored._store.close()
    legacy.write_text("invalid legacy must not override valid database")
    reopened = open_trainer(game, path, checkpoint=legacy, max_bytes=1)
    assert reopened.iterations == 20
    reopened._store.close()


def test_failed_migration_does_not_publish_or_modify_legacy(tmp_path):
    legacy, target = tmp_path / "old.json", tmp_path / "new.sqlite3"
    legacy.write_text('{"schema_version":2}')
    original = legacy.read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        open_trainer(KuhnPoker(), target, checkpoint=legacy)
    assert legacy.read_bytes() == original
    assert not target.exists()
    assert list(tmp_path.iterdir()) == [legacy]


def test_nonpluribus_and_oversized_import_are_rejected(tmp_path):
    legacy, target = tmp_path / "old.json", tmp_path / "new.sqlite3"
    Trainer(KuhnPoker()).save_checkpoint(legacy)
    with pytest.raises(ValueError, match="size limit"):
        open_trainer(KuhnPoker(), target, checkpoint=legacy, max_bytes=1)
    with pytest.raises(ValueError, match="Pluribus"):
        open_trainer(KuhnPoker(), target, checkpoint=legacy)
    assert not target.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("algorithm_version", "unknown"),
        ("rules_version", "other"),
        ("iterations", -1),
        ("chance_sampling", "game_exact"),
        ("seed", True),
    ],
)
def test_restore_rejects_incompatible_metadata_without_reset(tmp_path, field, value):
    path = tmp_path / "table.sqlite3"
    trainer = open_trainer(KuhnPoker(), path)
    trainer._store.close()
    rewrite_document(path, lambda d: d["metadata"].update({field: value}))
    digest = path.read_bytes()
    with pytest.raises(ValueError):
        open_trainer(KuhnPoker(), path)
    assert path.read_bytes() == digest


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(rng_state=[3, [0] * 625, float("nan")]),
        lambda d: d.update(rng_state=[True, [0] * 625, None]),
        lambda d: d.update(training_context={"stacks": [400, 800]}),
    ],
)
def test_restore_validates_rng_and_training_context(tmp_path, change):
    path = tmp_path / "table.sqlite3"
    trainer = open_trainer(KuhnPoker(), path)
    trainer._store.close()
    rewrite_document(path, change)
    with pytest.raises(ValueError):
        open_trainer(KuhnPoker(), path)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE training_entries SET regrets='[NaN,1]'",
        "UPDATE training_entries SET strategy_sum='[-1,1]'",
        'UPDATE training_entries SET actions=\'["check","check"]\'',
        "UPDATE training_entries SET stage=1",
        "UPDATE training_entries SET player=2",
        "UPDATE training_entries SET has_average=0",
        "UPDATE training_entries SET id=0",
        "PRAGMA user_version=9",
    ],
)
def test_restore_rejects_corrupt_entries_and_schema(tmp_path, sql):
    path = tmp_path / "table.sqlite3"
    trainer = open_trainer(KuhnPoker(), path)
    commit(trainer)
    trainer._store.close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(sql)
    with pytest.raises(ValueError):
        open_trainer(KuhnPoker(), path)


def test_restore_rejects_lookalike_schema_without_unique_information_sets(tmp_path):
    path = tmp_path / "table.sqlite3"
    trainer = open_trainer(KuhnPoker(), path)
    trainer._store.close()
    with sqlite3.connect(path) as db:
        definition = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='training_entries'"
        ).fetchone()[0]
        db.execute("DROP TABLE training_entries")
        db.execute(definition.replace(", UNIQUE(player,information_key)", ""))
    with pytest.raises(ValueError, match="constraints"):
        open_trainer(KuhnPoker(), path)


def test_corrupt_database_never_falls_back_to_checkpoint(tmp_path):
    legacy, target = tmp_path / "old.json", tmp_path / "broken.sqlite3"
    Trainer(KuhnPoker(), algorithm="pluribus").save_checkpoint(legacy)
    target.write_bytes(b"not a sqlite database")
    with pytest.raises(ValueError, match="database"):
        open_trainer(KuhnPoker(), target, checkpoint=legacy)
    assert target.read_bytes() == b"not a sqlite database"


def test_backup_captures_committed_wal_and_never_clobbers_output(tmp_path):
    source, target = tmp_path / "live.sqlite3", tmp_path / "snapshot.sqlite3"
    trainer = open_trainer(KuhnPoker(), source)
    commit(trainer)
    rng_state = trainer._rng.getstate()
    trainer._store.begin(1)
    trainer._store.set_entry((0, "first"), entry(9), 0)
    with pytest.raises(ValueError, match="committed"):
        trainer._store.backup(target)
    # An independent exporter may snapshot the prior committed iteration while a writer works.
    backup_checkpoint(source, target)
    restored = open_trainer(KuhnPoker(), target)
    assert restored.iterations == 1 and restored._rng.getstate() == rng_state
    assert restored._table[(0, "first")] == entry()
    restored._store.close()
    trainer._store.rollback()
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        trainer._store.backup(target)
    assert target.read_bytes() == before
    with pytest.raises(ValueError, match="differ"):
        trainer._store.backup(source)
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    with pytest.raises(ValueError, match="differ"):
        trainer._store.backup(alias)
    trainer._store.close()


def test_invalid_backup_leaves_no_output_or_temporary_file(tmp_path):
    source, target = tmp_path / "bad", tmp_path / "snapshot.sqlite3"
    source.write_text("invalid sqlite")
    with pytest.raises(ValueError):
        backup_checkpoint(source, target)
    assert set(tmp_path.iterdir()) == {source}


def test_restore_and_scan_do_not_materialize_the_full_table(tmp_path):
    path = tmp_path / "large.sqlite3"
    trainer = open_trainer(KuhnPoker(), path)
    trainer._store.begin(0)
    for index in range(6000):
        trainer._store.set_entry((index % 2, f"{index}:" + "x" * 2048), entry(), 0)
    trainer._store.commit(trainer, 1)
    trainer.iterations = 1
    trainer._store.close()
    tracemalloc.start()
    restored = open_trainer(KuhnPoker(), path)
    try:
        assert sum(1 for _ in restored._store.scan()) == 6000
        assert restored._store.counts() == (6000, 6000)
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 5 * 1024 * 1024
    finally:
        tracemalloc.stop()
        restored._store.close()
