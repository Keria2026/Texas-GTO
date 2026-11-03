"""The proposal switch preserves nonzero averages, regrets, RNG and rollback data."""

import sqlite3

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.cfr import Trainer
from texas_strategy.pluribus import MIXTURE_ALGORITHM_VERSION
from texas_strategy.training_store import open_trainer


def existing_model(tmp_path):
    game = KuhnPoker(3)
    source = Trainer(
        game,
        algorithm="pluribus",
        seed=42,
        pluribus={
            "average_mode": "sampled_uniform",
            "average_samples": 4,
            "sampled_average_from": 2,
            "discount_until": 0,
        },
    )
    source.train(5)
    checkpoint = tmp_path / "original.json"
    source.save_checkpoint(checkpoint)
    trainer = open_trainer(game, tmp_path / "training.sqlite3", checkpoint=checkpoint)
    return game, source, trainer


def test_upgrade_preserves_every_existing_value_and_records_future_switch(tmp_path):
    game, source, trainer = existing_model(tmp_path)
    store = trainer._store
    before = store._document
    counts = store.counts()
    assert counts[1] > 0
    backup = tmp_path / "before-mixture.sqlite3"
    store.backup(backup)
    try:
        store.enable_mixed_average(trainer)
        assert trainer.iterations == source.iterations == 5
        assert dict(trainer._table.items()) == source._table
        assert dict(trainer._betting_rounds) == source._betting_rounds
        assert trainer._rng.getstate() == source._rng.getstate()
        assert store.counts() == counts
        assert trainer.pluribus.mixture_from == 6
        assert trainer.pluribus.sampled_average_from == 2
        assert trainer.pluribus.average_samples == 4
        assert store._document["rng_state"] == before["rng_state"]
        assert store._document["training_context"] == before["training_context"]
        assert store._metadata["algorithm_version"] == MIXTURE_ALGORITHM_VERSION
        with pytest.raises(ValueError, match="uniform sampled"):
            store.enable_mixed_average(trainer)
    finally:
        store.close()
    original = open_trainer(game, backup)
    resumed = open_trainer(game, tmp_path / "training.sqlite3")
    try:
        assert original._store._document == before
        assert resumed.pluribus.mixture_from == 6
        assert resumed.train(3).completed_iterations == original.train(3).completed_iterations == 3
        for key in set(original._table) | set(resumed._table):
            left, right = original._table.get(key), resumed._table.get(key)
            actions = (left or right).actions
            assert (left.regrets if left else [0.0] * len(actions)) == (
                right.regrets if right else [0.0] * len(actions)
            )
        assert resumed._rng.getstate() == original._rng.getstate()
    finally:
        original._store.close()
        resumed._store.close()


def test_failed_metadata_write_restores_configuration_and_all_prior_state(tmp_path):
    _, source, trainer = existing_model(tmp_path)
    store = trainer._store
    before = store._document
    try:
        store._db.set_authorizer(
            lambda operation, table, *_: (
                sqlite3.SQLITE_DENY
                if operation == sqlite3.SQLITE_UPDATE and table == "training_meta"
                else sqlite3.SQLITE_OK
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            store.enable_mixed_average(trainer)
        store._db.set_authorizer(None)
        assert trainer.pluribus == source.pluribus
        assert store._document == before and not store._db.in_transaction
        assert dict(trainer._table.items()) == source._table
        assert trainer._rng.getstate() == source._rng.getstate()
        trainer.train(1)
        source.train(1)
        assert dict(trainer._table.items()) == source._table
    finally:
        store.close()


def test_upgrade_rejects_an_uncommitted_or_differently_configured_source(tmp_path):
    _, _, trainer = existing_model(tmp_path)
    try:
        trainer._rng.random()
        with pytest.raises(ValueError, match="committed averaging source"):
            trainer._store.enable_mixed_average(trainer)
    finally:
        trainer._store.close()
