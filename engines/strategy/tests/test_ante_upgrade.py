"""An ante upgrade preserves history without using legacy values as new coverage."""

import sqlite3
from copy import deepcopy

import pytest

from texas_strategy.cfr import Trainer, _TableEntry
from texas_strategy.mushroom_population import MushroomPopulationGame
from texas_strategy.persistent_policy import policy_snapshot
from texas_strategy.training_store import open_trainer


def population():
    return MushroomPopulationGame(
        [
            {
                "stacks": [64] * 8,
                "outstanding": [0] * 8,
                "waiting_for_top_up": [],
                "button": 0,
                "weight": 1,
            }
        ],
        horizon=1,
    )


def test_upgrade_keeps_rows_rng_and_checkpoint_portability_then_trains_distinct_rules(tmp_path):
    old = population()
    seed = tmp_path / "seed.json"
    Trainer(
        old,
        algorithm="pluribus",
        seed=17,
        pluribus={
            "average_mode": "sampled_mixture",
            "average_samples": 2,
            "snapshot_interval": 1,
            "discount_interval": 1,
        },
    ).save_checkpoint(seed)
    path = tmp_path / "training.sqlite3"
    trainer = open_trainer(old, path, checkpoint=seed)
    try:
        trainer.train(1)
        rows = deepcopy(dict(trainer._table.items()))
        rng = trainer._rng.getstate()
        old_document = deepcopy(trainer._store._document)
        backup = tmp_path / "before-ante.sqlite3"
        trainer._store.backup(backup)
        new = old.with_ante(source_iterations=trainer.iterations)
        trainer._store.enable_ante_training(trainer, new)
        assert dict(trainer._table.items()) == rows
        assert trainer._rng.getstate() == rng
        assert trainer.iterations == 1
        assert trainer.average_policy().entries == {}
        assert trainer.current_policy().entries == {}
        assert trainer._store._document["metadata"] == {
            **old_document["metadata"],
            "rules_version": new.rules_version,
        }
        assert (
            MushroomPopulationGame.from_context(new.checkpoint_context).checkpoint_context
            == new.checkpoint_context
        )
        with policy_snapshot(new, path) as policy:
            assert all(policy.lookup(*key) is None for key in rows)
        with pytest.raises(ValueError):
            with policy_snapshot(old, path):
                pass
        upgraded = tmp_path / "upgraded.json"
        trainer.save_checkpoint(upgraded)
        resumed = Trainer.load_checkpoint(new, upgraded)
        assert trainer.train(1).completed_iterations == resumed.train(1).completed_iterations == 1
        assert dict(trainer._table.items()) == resumed._table
        assert trainer._rng.getstate() == resumed._rng.getstate()
        assert all(trainer._table[key] == value for key, value in rows.items())
        new_keys = set(trainer._table) - rows.keys()
        assert new_keys and all(key[1].startswith("ante4:") for key in new_keys)
        assert all(key[1].startswith("ante4:") for key in trainer.average_policy().entries)
    finally:
        trainer._store.close()
    original = open_trainer(old, backup)
    try:
        assert original.iterations == 1
        assert dict(original._table.items()) == rows
        assert original._rng.getstate() == rng
    finally:
        original._store.close()


def test_invalid_or_stale_ante_upgrade_leaves_committed_store_intact(tmp_path):
    old = population()
    trainer = open_trainer(old, tmp_path / "training.sqlite3")
    try:
        before = deepcopy(trainer._store._document)
        wrong = old.with_ante(source_iterations=1)
        with pytest.raises(ValueError, match="committed source"):
            trainer._store.enable_ante_training(trainer, wrong)
        assert trainer.game is old and trainer._store._document == before
        target = old.with_ante(source_iterations=0)
        trainer._store.enable_ante_training(trainer, target)
        with pytest.raises(ValueError, match="committed source"):
            trainer._store.enable_ante_training(trainer, target)
        assert trainer.game is target
    finally:
        trainer._store.close()


def test_failed_metadata_write_rolls_back_rules_rng_and_transaction(tmp_path, monkeypatch):
    old = population()
    path = tmp_path / "training.sqlite3"
    trainer = open_trainer(old, path)
    store = trainer._store
    db = store._db
    before = deepcopy(store._document)

    class FailMigration:
        def __getattr__(self, name):
            return getattr(db, name)

        def execute(self, statement, *args):
            if statement.startswith("UPDATE training_meta"):
                raise sqlite3.OperationalError("synthetic metadata write failure")
            return db.execute(statement, *args)

    monkeypatch.setattr(store, "_db", FailMigration())
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            store.enable_ante_training(trainer, old.with_ante(source_iterations=0))
        assert store.game is trainer.game is old
        assert not db.in_transaction
        assert store._document == before
        assert store.counts() == (0, 0)
    finally:
        store.close()
    restored = open_trainer(old, path)
    try:
        assert restored._store._document == before
    finally:
        restored._store.close()


def test_legacy_rows_cannot_preseed_new_namespace_coverage(tmp_path):
    old = population()
    trainer = open_trainer(old, tmp_path / "training.sqlite3")
    try:
        store = trainer._store
        store.begin(0)
        store.set_entry(
            (0, "ante4:untrusted"), _TableEntry(("fold", "call"), [1.0, 2.0], [3.0, 4.0]), 0
        )
        store.commit(trainer, 1)
        trainer.iterations = 1
        before = deepcopy(store._document)
        with pytest.raises(ValueError, match="new ante namespace"):
            store.enable_ante_training(trainer, old.with_ante(source_iterations=1))
        assert trainer.game is old and store._document == before
        assert store.counts() == (1, 1)
    finally:
        trainer._store.close()
