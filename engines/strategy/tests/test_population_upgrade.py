"""Population warm starts explicitly retain a committed fixed-root model."""

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest

from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy.blueprint import load_checkpoint
from texas_strategy.cfr import Trainer, _TableEntry
from texas_strategy.mushroom_abstraction import MushroomContextGame
from texas_strategy.mushroom_population import MushroomPopulationGame
from texas_strategy.persistent_policy import policy_snapshot
from texas_strategy.policy import legal_information
from texas_strategy.training_store import open_trainer


def population(previous, iteration, *, weight=1, horizon=None):
    base = previous.base_game
    roots = [
        {
            "weight": weight,
            "stacks": list(base.initial_stacks),
            "outstanding": list(base.initial_outstanding),
            "waiting_for_top_up": list(base.initial_waiting_for_top_up),
            "button": base.button,
        },
        {
            "weight": 3,
            "stacks": [64] * 4 + [0] * 4,
            "outstanding": [20] * 8,
            "waiting_for_top_up": [4, 5, 6, 7],
            "button": 0,
        },
    ]
    return MushroomPopulationGame(
        roots=roots,
        horizon=base.horizon if horizon is None else horizon,
        warm_start={
            "previous_context": previous.checkpoint_context,
            "source_iterations": iteration,
            "schedule": "continue_total_iterations",
        },
    )


def rows(store):
    return store._db.execute("SELECT * FROM training_entries ORDER BY id").fetchall()


@pytest.fixture
def fixed(tmp_path):
    game = MushroomContextGame(MushroomSessionGame((64,) * 8, horizon=1))
    trainer = open_trainer(game, tmp_path / "training.sqlite3")
    store = trainer._store
    store.begin(0)
    state = game.initial_state()
    stages = set()
    while stages != {0, 1}:
        actor = game.actor(state)
        if actor == -1:
            action, _ = game.sample_chance_outcome(state, trainer._rng)
        else:
            key, actions = legal_information(game, state)
            stage = game.betting_round(state)
            if stage not in stages:
                store.set_entry(
                    key,
                    _TableEntry(
                        actions,
                        [-3.5] + [2.0] * (len(actions) - 1),
                        [4.0 if stage == 0 else 0.0] + [0.0] * (len(actions) - 1),
                    ),
                    stage,
                )
                stages.add(stage)
            action = "check" if "check" in actions else "call"
        state = game.transition(state, action)
    store.commit(trainer, 1)
    trainer.iterations = 1
    try:
        yield trainer
    finally:
        store.close()


def test_explicit_upgrade_retains_every_row_rng_schedule_and_old_backup(fixed, tmp_path):
    store, previous = fixed._store, fixed.game
    original, document = rows(store), deepcopy(store._document)
    rng = fixed._rng.getstate()
    backup = tmp_path / "before.sqlite3"
    store.backup(backup)
    target = population(previous, fixed.iterations)
    statements = []
    store._db.set_trace_callback(statements.append)
    store.enable_population_training(fixed, target)
    store._db.set_trace_callback(None)
    assert fixed.game is store.game is target
    assert fixed.iterations == store._iterations == 1
    assert fixed._rng.getstate() == rng
    assert rows(store) == original
    assert store.counts() == (2, 1)
    assert {**store._document, "training_context": document["training_context"]} == document
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert not any("training_entries" in sql.lower() for sql in statements)
    reopened = open_trainer(
        MushroomPopulationGame.from_context(target.checkpoint_context), store.path
    )
    restored = open_trainer(previous, backup)
    try:
        assert rows(reopened._store) == rows(restored._store) == original
        assert reopened._rng.getstate() == restored._rng.getstate() == rng
        assert reopened.iterations == restored.iterations == 1
        assert reopened.pluribus == restored.pluribus == fixed.pluribus
        assert restored._store._document == document
    finally:
        reopened._store.close()
        restored._store.close()
    with pytest.raises(ValueError, match="context"):
        open_trainer(previous, store.path)
    with pytest.raises(ValueError, match="context"):
        open_trainer(population(previous, 1, weight=2), store.path)


def test_population_upgrade_cannot_repeat_or_be_used_by_foreign_trainer(fixed):
    previous, store = fixed.game, fixed._store
    target = population(previous, 1)
    with pytest.raises(ValueError, match="own"):
        store.enable_population_training(Trainer(previous, algorithm="pluribus"), target)
    with pytest.raises(ValueError, match="population game"):
        store.enable_population_training(fixed, previous)
    store.enable_population_training(fixed, target)
    committed = deepcopy(store._document)
    with pytest.raises(ValueError):
        store.enable_population_training(fixed, target)
    assert store._document == committed
    assert fixed.game is store.game is target


@pytest.mark.parametrize("fault", ["iteration", "horizon", "rng", "seed", "pluribus", "family"])
def test_migration_rejects_different_source_or_model_semantics(fixed, fault):
    store, previous = fixed._store, fixed.game
    before, document = rows(store), deepcopy(store._document)
    target = population(
        previous, 2 if fault == "iteration" else 1, horizon=2 if fault == "horizon" else 1
    )
    if fault == "rng":
        fixed._rng.random()
    elif fault == "seed":
        fixed.seed += 1
    elif fault == "pluribus":
        fixed.pluribus = replace(fixed.pluribus, average_samples=32)
    elif fault == "family":
        target.rules_version += ":changed"
    with pytest.raises(ValueError):
        store.enable_population_training(fixed, target)
    assert fixed.game is store.game is previous
    assert store._document == document
    assert rows(store) == before
    assert store.counts() == (2, 1)
    assert not store._db.in_transaction


@pytest.mark.parametrize("denied", ["metadata", "commit"])
def test_failed_migration_restores_game_metadata_and_counts(fixed, denied):
    store, previous = fixed._store, fixed.game
    before, document, rng = rows(store), deepcopy(store._document), fixed._rng.getstate()
    target = population(previous, 1)
    store._db.set_authorizer(
        lambda action, name, *_: (
            sqlite3.SQLITE_DENY
            if (
                denied == "metadata" and action == sqlite3.SQLITE_UPDATE and name == "training_meta"
            )
            or (denied == "commit" and action == sqlite3.SQLITE_TRANSACTION and name == "COMMIT")
            else sqlite3.SQLITE_OK
        )
    )
    with pytest.raises(sqlite3.DatabaseError):
        store.enable_population_training(fixed, target)
    store._db.set_authorizer(None)
    assert fixed.game is store.game is previous
    assert store._document == document
    assert rows(store) == before
    assert fixed._rng.getstate() == rng
    assert store.counts() == (2, 1)
    assert not store._db.in_transaction
    store.enable_population_training(fixed, target)
    assert store.counts() == (2, 1)


def test_other_committed_writer_invalidates_migration_before_game_changes(fixed):
    store, previous = fixed._store, fixed.game
    other = open_trainer(previous, store.path)
    try:
        other._store.begin(other.iterations)
        other._store.commit(other, other.iterations + 1)
        other.iterations += 1
        with pytest.raises(ValueError, match="Stale"):
            store.enable_population_training(fixed, population(previous, 1))
        assert fixed.game is store.game is previous
        assert not store._db.in_transaction
    finally:
        other._store.close()


def test_upgrade_does_not_take_over_an_active_training_transaction(fixed):
    store, previous = fixed._store, fixed.game
    store.begin(fixed.iterations)
    try:
        with pytest.raises(ValueError, match="already active"):
            store.enable_population_training(fixed, population(previous, 1))
        assert fixed.game is store.game is previous
        assert store._db.in_transaction
        assert store.counts() == (2, 1)
    finally:
        store.rollback()


def test_reader_keeps_old_snapshot_while_new_header_publishes(fixed):
    store, previous = fixed._store, fixed.game
    with policy_snapshot(previous, store.path) as snapshot:
        digest = snapshot.metadata_digest
        store.enable_population_training(fixed, population(previous, 1))
        assert snapshot.metadata_digest == digest
        with policy_snapshot(previous, store.path) as updated:
            assert updated.metadata_digest != digest
            assert updated.iterations == snapshot.iterations == 1


def test_unknown_manifest_version_cannot_be_resumed(fixed):
    context = json.loads(population(fixed.game, 1).checkpoint_context)
    context["version"] = "unknown-population-version"
    with pytest.raises(ValueError):
        MushroomPopulationGame.from_context(json.dumps(context))


def test_unknown_manifest_cannot_be_published_from_an_altered_population(fixed):
    store, previous = fixed._store, fixed.game
    target = population(previous, 1)
    context = json.loads(target.checkpoint_context)
    context["version"] = "unknown-population-version"
    target.checkpoint_context = json.dumps(context, sort_keys=True, separators=(",", ":"))
    document = deepcopy(store._document)
    with pytest.raises(ValueError):
        store.enable_population_training(fixed, target)
    assert store._document == document
    assert fixed.game is store.game is previous


def test_new_context_survives_portable_exports_and_deterministic_resume(fixed, tmp_path):
    store = fixed._store
    target = population(fixed.game, 1)
    store.enable_population_training(fixed, target)
    backup, checkpoint = tmp_path / "warm.sqlite3", tmp_path / "warm.json"
    store.backup(backup)
    fixed.save_checkpoint(checkpoint)
    loaded = load_checkpoint(
        MushroomPopulationGame.from_context(target.checkpoint_context), checkpoint
    )
    assert loaded.game.checkpoint_context == target.checkpoint_context
    assert loaded._rng.getstate() == fixed._rng.getstate()
    assert dict(loaded._table.items()) == dict(store.items())
    continuation = open_trainer(
        MushroomPopulationGame.from_context(target.checkpoint_context), backup
    )
    try:
        assert fixed.train(1).completed_iterations == 1
        assert continuation.train(1).completed_iterations == 1
        assert continuation._store._document == store._document
        assert rows(continuation._store) == rows(store)
        assert continuation._rng.getstate() == fixed._rng.getstate()
        assert json.loads(target.checkpoint_context)["warm_start"]["source_iterations"] == 1
        assert fixed.iterations == 2
    finally:
        continuation._store.close()


def test_warm_start_cannot_create_an_empty_model_with_claimed_progress(fixed, tmp_path):
    target = population(fixed.game, 5)
    database = tmp_path / "invalid-empty.sqlite3"
    with pytest.raises(ValueError, match="precede"):
        open_trainer(target, database)
    assert not database.exists()
    checkpoint = tmp_path / "invalid-empty.json"
    with pytest.raises(ValueError, match="precede"):
        Trainer(target, algorithm="pluribus").save_checkpoint(checkpoint)
    assert not checkpoint.exists()
