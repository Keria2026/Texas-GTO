"""Constant-time writer counts stay aligned with durable transactions and snapshots."""

import sqlite3
from contextlib import closing

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.cfr import _TableEntry
from texas_strategy.persistent_policy import policy_snapshot
from texas_strategy.training_store import TrainingStore, backup_checkpoint, open_trainer


def entry(average=1):
    return _TableEntry(("check", "bet"), [-1, 1], [average, 0])


def commit(trainer):
    trainer._store.commit(trainer, trainer.iterations + 1)
    trainer.iterations += 1


@pytest.fixture
def trained(tmp_path):
    trainer = open_trainer(KuhnPoker(), tmp_path / "training.sqlite3")
    trainer._store.begin(0)
    trainer._store.set_entry((0, "first"), entry(), 0)
    trainer._store.set_entry((1, "second"), entry(0), 0)
    commit(trainer)
    try:
        yield trainer
    finally:
        trainer._store.close()


def test_counts_and_len_do_not_read_training_entries(trained):
    store = trained._store
    statements = []
    store._db.set_trace_callback(statements.append)
    store._db.set_authorizer(
        lambda action, table, *_: (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table == "training_entries"
            else sqlite3.SQLITE_OK
        )
    )
    for _ in range(5):
        assert store.counts() == (2, 1)
        assert len(store) == 2
    assert statements and all("training_entries" not in sql for sql in statements)


def test_insert_overwrite_average_clear_and_rollback(trained):
    store = trained._store
    store.begin(1)
    store.set_entry((0, "third"), entry(), 0)
    store.set_entry((0, "third"), entry(3), 0)
    assert store.counts() == (3, 2)
    store.set_entry((0, "first"), entry(0), 0)
    store.set_entry((1, "second"), entry(7), 0)
    assert store.counts() == (3, 2)
    store.set_entry((1, "zero"), _TableEntry(("check", "bet"), [0, 0], [0, 0]), 0)
    assert store.counts() == (3, 2)
    store.rollback()
    assert store.counts() == (2, 1)
    store.begin(1)
    store.set_entry((0, "first"), entry(0), 0)
    commit(trained)
    assert store.counts() == (2, 0)


MUTATIONS = (
    "INSERT INTO training_entries(player,information_key,actions,regrets,strategy_sum,"
    "stage,has_average) VALUES(0,'third','[\"check\",\"bet\"]','[-1,1]','[1,0]',0,1)",
    "UPDATE training_entries SET strategy_sum='[0,0]',has_average=0 WHERE information_key='first'",
    "DELETE FROM training_entries WHERE information_key='first'",
)


@pytest.mark.parametrize(
    "sql,expected", tuple(zip(MUTATIONS, ((3, 2), (2, 0), (1, 0)), strict=True))
)
@pytest.mark.parametrize("external", [False, True])
def test_external_sql_is_revalidated_and_untracked_local_sql_requires_reopen(
    trained, sql, expected, external
):
    store = trained._store
    if external:
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute(sql)
    else:
        store._db.execute(sql)
    if external:
        assert store.counts() == expected
        store.begin(1)
        store.rollback()
        assert store.counts() == expected
    else:
        for _ in range(2):
            with pytest.raises(ValueError, match="Stale training counts"):
                store.counts()
            with pytest.raises(ValueError, match="Stale training counts"):
                store.begin(1)
            store.rollback()
    reopened = open_trainer(KuhnPoker(), store.path)
    try:
        assert reopened._store.counts() == expected
        assert reopened.iterations == 1
    finally:
        reopened._store.close()


def test_untracked_sql_inside_owned_transaction_can_only_be_rolled_back(trained):
    store = trained._store
    store.begin(1)
    store.set_entry((0, "third"), entry(), 0)
    store._db.execute(MUTATIONS[2])
    with pytest.raises(ValueError, match="untracked write"):
        store.counts()
    with pytest.raises(ValueError, match="untracked write"):
        store.commit(trained, 2)
    store.rollback()
    assert store.counts() == (2, 1)
    assert trained.iterations == 1


def test_caller_owned_sql_transaction_cannot_bypass_count_snapshot(trained):
    store = trained._store
    store._db.execute("BEGIN")
    with pytest.raises(ValueError, match="active training transaction"):
        store.set_entry((0, "third"), entry(), 0)
    with pytest.raises(ValueError, match="active training transaction"):
        store.commit(trained, 2)
    store.rollback()
    assert store.counts() == (2, 1)


@pytest.mark.parametrize("sql,expected", [("ROLLBACK", (2, 1)), ("COMMIT", (2, 0))])
def test_raw_transaction_exit_cannot_return_or_revalidate_wrong_counts(trained, sql, expected):
    store = trained._store
    store.begin(1)
    store.set_entry((0, "first"), entry(0), 0)
    store._db.execute(sql)
    with pytest.raises(ValueError, match="untracked transaction"):
        store.counts()
    with pytest.raises(ValueError, match="Stale training transaction"):
        store.begin(1)
    store.rollback()
    with pytest.raises(ValueError, match="untracked transaction"):
        store.counts()
    reopened = open_trainer(KuhnPoker(), store.path)
    try:
        assert reopened._store.counts() == expected
    finally:
        reopened._store.close()


def test_readonly_snapshot_and_backup_do_not_invalidate_writer_counts(trained, tmp_path):
    store = trained._store
    with policy_snapshot(KuhnPoker(), store.path) as policy:
        assert policy.iterations == 1
        assert policy.lookup(0, "first") is not None
        store.begin(1)
        store.set_entry((0, "first"), entry(0), 0)
        store.set_entry((0, "third"), entry(), 0)
        assert store.counts() == (3, 1)
        backup = tmp_path / "before-commit.sqlite3"
        backup_checkpoint(store.path, backup)
        commit(trained)
        assert store.counts() == (3, 1)
        assert policy.lookup(0, "first") is not None
    assert store.counts() == (3, 1)
    restored = open_trainer(KuhnPoker(), backup)
    try:
        assert restored.iterations == 1
        assert restored._store.counts() == (2, 1)
    finally:
        restored._store.close()


def test_existing_schema_reopens_with_one_validation_scan_and_no_aggregate(trained, monkeypatch):
    path = trained._store.path
    statements, scans = [], []
    original_connect, original_scan = sqlite3.connect, TrainingStore.scan

    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    def scan(self):
        scans.append(self.path)
        yield from original_scan(self)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(TrainingStore, "scan", scan)
    reopened = open_trainer(KuhnPoker(), path)
    try:
        assert reopened._store.counts() == (2, 1)
        assert scans == [path]
        assert not any("COUNT(" in sql.upper() or "SUM(" in sql.upper() for sql in statements)
        assert reopened._store._db.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall() == [("table", "training_entries"), ("table", "training_meta")]
    finally:
        reopened._store.close()


def test_commit_during_initial_scan_never_relabels_stale_counts(trained, monkeypatch):
    original_scan = TrainingStore.scan
    scans = []

    def scan(self):
        scans.append(self.path)
        yield from original_scan(self)
        if len(scans) == 1:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute(MUTATIONS[0])

    monkeypatch.setattr(TrainingStore, "scan", scan)
    reopened = open_trainer(KuhnPoker(), trained._store.path)
    try:
        assert reopened._store.counts() == (3, 2)
        assert len(scans) == 2
    finally:
        reopened._store.close()


def test_bad_rows_in_legacy_delete_journal_are_rejected_without_changing_data(trained, tmp_path):
    path = tmp_path / "legacy.sqlite3"
    backup_checkpoint(trained._store.path, path)
    with closing(sqlite3.connect(path)) as db, db:
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        db.execute("UPDATE training_entries SET strategy_sum='[-1,0]'")
        before = db.execute("SELECT * FROM training_entries ORDER BY id").fetchall()
        metadata = db.execute("SELECT * FROM training_meta").fetchall()
    with pytest.raises(ValueError, match="nonnegative"):
        open_trainer(KuhnPoker(), path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT * FROM training_entries ORDER BY id").fetchall() == before
        assert db.execute("SELECT * FROM training_meta").fetchall() == metadata


def test_external_commit_at_begin_is_revalidated_after_write_lock(trained):
    store = trained._store
    original = store._db

    class RaceConnection:
        def __getattr__(self, name):
            return getattr(original, name)

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                with closing(sqlite3.connect(store.path)) as other, other:
                    other.execute(MUTATIONS[0])
            return original.execute(sql, *args)

    store._db = RaceConnection()
    store.begin(1)
    assert store.counts() == (3, 2)
    store.rollback()
    assert not original.in_transaction
    assert store.counts() == (3, 2)


def test_external_wal_truncation_refreshes_once_then_counts_remain_constant_time(
    trained, monkeypatch
):
    store = trained._store
    old_version = store._data_version
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)
    assert store._db.execute("PRAGMA data_version").fetchone()[0] != old_version
    original_scan, scans = store.scan, []

    def scan():
        scans.append(True)
        yield from original_scan()

    monkeypatch.setattr(store, "scan", scan)
    assert store.counts() == (2, 1)
    for _ in range(4):
        assert len(store) == 2
    store.begin(1)
    store.set_entry((0, "third"), entry(), 0)
    commit(trained)
    assert store.counts() == (3, 2)
    assert len(scans) == 1


def test_external_training_commit_still_rejects_stale_writer(trained):
    other = open_trainer(KuhnPoker(), trained._store.path)
    try:
        other._store.begin(1)
        other._store.set_entry((0, "third"), entry(), 0)
        commit(other)
        with pytest.raises(ValueError, match="Stale training writer"):
            trained._store.counts()
        assert not trained._store._db.in_transaction
    finally:
        other._store.close()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE training_entries SET has_average=0 WHERE information_key='first'",
        "UPDATE training_entries SET id=0 WHERE information_key='first'",
    ],
)
def test_external_corruption_is_not_accepted_by_count_refresh(trained, sql):
    store = trained._store
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("PRAGMA ignore_check_constraints=ON")
        db.execute(sql)
    with pytest.raises(ValueError, match="Invalid training"):
        store.counts()
    assert not store._db.in_transaction


def test_second_commit_during_count_refresh_never_certifies_the_old_snapshot(trained, monkeypatch):
    store = trained._store
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute(MUTATIONS[0])
    original_scan = store.scan

    def scan():
        yield from original_scan()
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute(MUTATIONS[1])

    monkeypatch.setattr(store, "scan", scan)
    with pytest.raises(ValueError, match="during count refresh"):
        store.counts()
    monkeypatch.setattr(store, "scan", original_scan)
    assert store.counts() == (3, 1)
