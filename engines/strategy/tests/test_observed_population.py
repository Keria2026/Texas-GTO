"""Changing the public curriculum preserves one store, not a claim of strategy strength."""

import json
import sqlite3
from copy import deepcopy

import pytest

from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy.cfr import Trainer
from texas_strategy.mushroom_abstraction import MushroomContextGame
from texas_strategy.mushroom_population import MushroomPopulationGame, public_opening
from texas_strategy.training_store import open_trainer


def opening(cash=64, *, button=0):
    return public_opening(
        MushroomContextGame(MushroomSessionGame((cash,) * 8, button=button, ante=4, horizon=1))
    )


def population():
    return MushroomPopulationGame([{**opening(), "weight": 100}], ante=4, horizon=1)


def test_public_finances_rotate_without_private_cards_or_changing_money():
    cash = (101, 203, 307, 409, 503, 607, 709, 811)
    rights = (20, 40, 60, 80, 100, 120, 140, 160)
    game = MushroomContextGame(MushroomSessionGame(cash, button=3, outstanding=rights, ante=4))
    root = public_opening(game)
    assert root == {
        "weight": 1,
        "stacks": list(cash[3:] + cash[:3]),
        "outstanding": list(rights[3:] + rights[:3]),
        "waiting_for_top_up": [],
        "button": 0,
    }
    assert opening(button=0) == opening(button=5)


def test_append_deduplicate_sampler_mass_and_self_contained_history():
    original = population()
    first = original.with_openings([opening(80), opening(80)], source_iterations=7)
    assert first.roots[0] == original.roots[0]
    assert first.observed_opening_count == 1
    assert dict(first.chance_outcomes(first.initial_state())) == {"root:0": 0.5, "root:1": 0.5}
    assert first.with_openings([opening(80), opening()], source_iterations=9) is first
    second = first.with_openings([opening(96)], source_iterations=9)
    assert [r["weight"] for r in second.roots] == [100, 50, 50]
    assert second.opening_updates["steps"] == [
        {"iterations": 7, "count": 1},
        {"iterations": 9, "count": 2},
    ]
    assert second.opening_updates["source_context"] == original.checkpoint_context
    restored = MushroomPopulationGame.from_context(second.checkpoint_context)
    assert restored.checkpoint_context == second.checkpoint_context
    restored.validate_opening_update(first, source_iterations=9)
    with pytest.raises(ValueError):
        restored.validate_checkpoint_iterations(8)
    with pytest.raises(ValueError):
        restored.validate_opening_update(first, source_iterations=10)
    with pytest.raises(ValueError):
        restored.validate_opening_update(second, source_iterations=9)


def test_observed_curriculum_retains_prior_ante_migration_history():
    legacy = MushroomPopulationGame([{**opening(), "weight": 100}], horizon=1)
    ante = legacy.with_ante(source_iterations=11)
    observed = ante.with_openings([opening(80)], source_iterations=13)
    restored = MushroomPopulationGame.from_context(observed.checkpoint_context)
    assert restored.rule_migration == ante.rule_migration
    assert restored.opening_updates["source_context"] == ante.checkpoint_context
    restored.validate_opening_update(ante, source_iterations=13)
    restored.validate_checkpoint_iterations(13)
    with pytest.raises(ValueError):
        restored.validate_checkpoint_iterations(12)
    with pytest.raises(ValueError):
        ante.with_openings([opening(80)], source_iterations=10)


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v["roots"][0].update(weight=99),
        lambda v: v["roots"][1].update(weight=99),
        lambda v: v["opening_updates"]["steps"][0].update(count=0),
        lambda v: v["opening_updates"]["steps"][0].update(iterations=-1),
        lambda v: v.update(ante=0),
        lambda v: v.update(opening_updates=None),
    ],
)
def test_corrupt_population_updates_are_rejected(change):
    game = population().with_openings([opening(80)], source_iterations=7)
    data = json.loads(game.checkpoint_context)
    change(data)
    with pytest.raises(ValueError):
        MushroomPopulationGame.from_context(json.dumps(data, sort_keys=True, separators=(",", ":")))


def test_update_preserves_rows_rng_and_deterministic_portable_continuation(tmp_path):
    old = population()
    seed = tmp_path / "seed.json"
    Trainer(
        old,
        algorithm="pluribus",
        seed=17,
        pluribus={"average_mode": "sampled_mixture", "average_samples": 2},
    ).save_checkpoint(seed)
    trainer = open_trainer(old, tmp_path / "training.sqlite3", checkpoint=seed)
    try:
        trainer.train(1)
        before = deepcopy(trainer._store._document)
        rows = deepcopy(dict(trainer._table.items()))
        new = old.with_openings([opening(80)], source_iterations=1)
        trainer._store.enable_population_training(trainer, new)
        assert {
            **trainer._store._document,
            "training_context": before["training_context"],
        } == before
        assert dict(trainer._table.items()) == rows
        checkpoint = tmp_path / "observed.json"
        trainer.save_checkpoint(checkpoint)
        reference = Trainer.load_checkpoint(
            MushroomPopulationGame.from_context(new.checkpoint_context), checkpoint
        )
        trainer.train(2)
        reference.train(2)
        assert dict(trainer._table.items()) == reference._table
        assert trainer._rng.getstate() == reference._rng.getstate()
    finally:
        trainer._store.close()
    resumed = open_trainer(
        MushroomPopulationGame.from_context(new.checkpoint_context), tmp_path / "training.sqlite3"
    )
    try:
        resumed.train(1)
        reference.train(1)
        assert dict(resumed._table.items()) == reference._table
        assert resumed._rng.getstate() == reference._rng.getstate()
    finally:
        resumed._store.close()


def test_failed_update_restores_game_metadata_and_rng(tmp_path, monkeypatch):
    old = population()
    trainer = open_trainer(old, tmp_path / "training.sqlite3")
    store, db = trainer._store, trainer._store._db
    before = deepcopy(store._document)

    class FailUpdate:
        def __getattr__(self, name):
            return getattr(db, name)

        def execute(self, statement, *args):
            if statement.startswith("UPDATE training_meta"):
                raise sqlite3.OperationalError("synthetic metadata failure")
            return db.execute(statement, *args)

    monkeypatch.setattr(store, "_db", FailUpdate())
    try:
        with pytest.raises(sqlite3.OperationalError, match="synthetic"):
            store.enable_population_training(
                trainer, old.with_openings([opening(80)], source_iterations=0)
            )
        assert trainer.game is store.game is old
        assert store._document == before and not db.in_transaction
        assert list(trainer._rng.getstate()[1]) == before["rng_state"][1]
    finally:
        store.close()
