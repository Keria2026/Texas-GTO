"""An explicit averaging transition preserves committed regrets and prior artifacts."""

import sqlite3

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.cfr import Trainer
from texas_strategy.training_store import open_trainer


def legacy(tmp_path, *, interval=10000):
    game = KuhnPoker()
    trainer = Trainer(game, algorithm="pluribus", seed=42, pluribus={"strategy_interval": interval})
    trainer.train(3)
    checkpoint = tmp_path / "old.json"
    trainer.save_checkpoint(checkpoint)
    return game, trainer, checkpoint


def test_explicit_upgrade_preserves_every_value_rng_and_backup(tmp_path):
    game, before, checkpoint = legacy(tmp_path)
    saved_json = checkpoint.read_bytes()
    database = tmp_path / "training.sqlite3"
    trainer = open_trainer(game, database, checkpoint=checkpoint)
    backup = tmp_path / "before.sqlite3"
    trainer._store.backup(backup)
    try:
        trainer._store.enable_sampled_average(trainer)
        assert trainer.iterations == before.iterations == 3
        assert dict(trainer._table.items()) == before._table
        assert trainer._rng.getstate() == before._rng.getstate()
        assert dict(trainer._betting_rounds) == before._betting_rounds
        assert trainer.pluribus.average_mode == "sampled_uniform"
        assert trainer.pluribus.average_samples == 16
        assert trainer.pluribus.sampled_average_from == 4
        assert trainer.pluribus.strategy_interval == 1
        assert trainer.pluribus.snapshot_interval == before.pluribus.snapshot_interval
        with pytest.raises(ValueError, match="already configured"):
            trainer._store.enable_sampled_average(trainer)
    finally:
        trainer._store.close()
    assert checkpoint.read_bytes() == saved_json
    original = open_trainer(game, backup)
    upgraded = open_trainer(game, database)
    try:
        assert original.pluribus == before.pluribus
        assert dict(original._table.items()) == before._table
        assert original._rng.getstate() == before._rng.getstate()
        assert upgraded.pluribus.sampled_average_from == 4
        assert upgraded.train(1).completed_iterations == 1
        assert before.train(1).completed_iterations == 1
        for key, entry in before._table.items():
            assert upgraded._table[key].regrets == entry.regrets
        assert upgraded._rng.getstate() == before._rng.getstate()
        assert upgraded._store.counts()[1] > 0
    finally:
        original._store.close()
        upgraded._store.close()


def test_nonzero_preflop_averages_are_never_reset_or_silently_mixed(tmp_path):
    game, before, checkpoint = legacy(tmp_path, interval=1)
    trainer = open_trainer(game, tmp_path / "training.sqlite3", checkpoint=checkpoint)
    try:
        with pytest.raises(ValueError, match="silently reweighted"):
            trainer._store.enable_sampled_average(trainer)
        assert trainer.pluribus == before.pluribus
        assert trainer._store._document["metadata"]["pluribus"] == before.pluribus.to_dict()
        assert dict(trainer._table.items()) == before._table
        assert trainer._rng.getstate() == before._rng.getstate()
        assert trainer.train(1).completed_iterations == 1
        assert before.train(1).completed_iterations == 1
        assert dict(trainer._table.items()) == before._table
    finally:
        trainer._store.close()


def test_metadata_write_failure_restores_original_configuration(tmp_path):
    game, before, checkpoint = legacy(tmp_path)
    trainer = open_trainer(game, tmp_path / "training.sqlite3", checkpoint=checkpoint)
    try:
        trainer._store._db.set_authorizer(
            lambda action, table, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_UPDATE and table == "training_meta"
                else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            trainer._store.enable_sampled_average(trainer)
        trainer._store._db.set_authorizer(None)
        assert trainer.pluribus == before.pluribus
        assert not trainer._store._db.in_transaction
        assert dict(trainer._table.items()) == before._table
        assert trainer._rng.getstate() == before._rng.getstate()
        assert trainer.train(1).completed_iterations == 1
    finally:
        trainer._store.close()
