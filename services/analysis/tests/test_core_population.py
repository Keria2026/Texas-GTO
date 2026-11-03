import json
import sqlite3

import pytest

from texas_analysis.core_training import CORE_GAME, LEGACY_CORE_GAME, _open_core_trainer
from texas_analysis.game_factory import make_game
from texas_analysis.models import GameConfig
from texas_strategy.cfr import Trainer
from texas_strategy.mushroom_population import MushroomPopulationGame
from texas_strategy.training_store import TrainingStore, open_trainer


def header(path):
    with sqlite3.connect(path) as db:
        return json.loads(db.execute("SELECT document FROM training_meta").fetchone()[0])


def test_worker_upgrades_one_core_once_and_restores_frozen_manifest(tmp_path):
    path = tmp_path / "training.sqlite3"
    legacy = make_game(LEGACY_CORE_GAME)
    old = open_trainer(legacy, path)
    before = old._store._document
    old._store.close()
    new = _open_core_trainer(tmp_path)
    try:
        assert isinstance(new.game, MushroomPopulationGame)
        assert new.game.warm_start["source_iterations"] == 0
        assert new._store._document["metadata"] == before["metadata"]
        assert new._store._document["rng_state"] == before["rng_state"]
        context = new.game.checkpoint_context
    finally:
        new._store.close()
    backups = list(tmp_path.glob("before-population-*.sqlite3"))
    assert len(backups) == 1 and header(backups[0]) == before
    resumed = _open_core_trainer(tmp_path)
    try:
        assert resumed.game.checkpoint_context == context
    finally:
        resumed._store.close()
    assert list(tmp_path.glob("before-population-*.sqlite3")) == backups


@pytest.mark.parametrize("population", [False, True])
def test_json_import_preserves_original_artifact_and_distribution(tmp_path, population):
    game = make_game(CORE_GAME if population else LEGACY_CORE_GAME)
    checkpoint = tmp_path / "checkpoint.json"
    Trainer(game, algorithm="pluribus", seed=17).save_checkpoint(checkpoint)
    before = checkpoint.read_bytes()
    trainer = _open_core_trainer(tmp_path)
    try:
        assert isinstance(trainer.game, MushroomPopulationGame)
        assert (
            trainer.game.warm_start is None if population else trainer.game.warm_start is not None
        )
        assert trainer.seed == 17
        assert trainer.iterations == 0
    finally:
        trainer._store.close()
    assert checkpoint.read_bytes() == before


def test_unknown_existing_context_is_never_reset(tmp_path):
    path = tmp_path / "training.sqlite3"
    trainer = _open_core_trainer(tmp_path)
    trainer._store.close()
    document = header(path)
    value = json.loads(document["training_context"])
    value["version"] = "future-population"
    document["training_context"] = json.dumps(value, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE training_meta SET document=?", (json.dumps(document),))
    with pytest.raises(ValueError, match="Unsupported"):
        _open_core_trainer(tmp_path)
    assert header(path) == document
    assert not list(tmp_path.glob("before-population-*.sqlite3"))


def test_failed_backup_leaves_fixed_opening_context_untouched(tmp_path, monkeypatch):
    path = tmp_path / "training.sqlite3"
    old = open_trainer(make_game(LEGACY_CORE_GAME), path)
    before = old._store._document
    old._store.close()

    def fail(*_):
        raise OSError("synthetic backup failure")

    monkeypatch.setattr(TrainingStore, "backup", fail)
    with pytest.raises(OSError, match="synthetic"):
        _open_core_trainer(tmp_path)
    assert header(path) == before


@pytest.mark.parametrize(
    "change",
    [
        {"num_players": 4},
        {"stacks": [800] * 8},
        {"button": 1},
        {"stack": 400},
    ],
)
def test_population_config_does_not_silently_ignore_fixed_opening_overrides(change):
    with pytest.raises(ValueError, match="Population"):
        GameConfig.model_validate({**CORE_GAME.model_dump(), **change})


def test_uniform_core_proposal_is_backed_up_and_upgraded_exactly_once(tmp_path):
    game = make_game(CORE_GAME)
    checkpoint = tmp_path / "checkpoint.json"
    Trainer(
        game,
        algorithm="pluribus",
        seed=42,
        pluribus={"average_mode": "sampled_uniform", "average_samples": 16},
    ).save_checkpoint(checkpoint)
    previous = open_trainer(game, tmp_path / "training.sqlite3", checkpoint=checkpoint)
    before = previous._store._document
    previous._store.close()
    upgraded = _open_core_trainer(tmp_path)
    try:
        assert upgraded.pluribus.average_mode == "sampled_mixture"
        assert upgraded.pluribus.mixture_from == 1
        assert upgraded._store._document["rng_state"] == before["rng_state"]
        assert upgraded._store._document["training_context"] == before["training_context"]
        after = upgraded._store._document
    finally:
        upgraded._store.close()
    backups = list(tmp_path.glob("before-mixture-*.sqlite3"))
    assert len(backups) == 1 and header(backups[0]) == before
    resumed = _open_core_trainer(tmp_path)
    resumed._store.close()
    assert header(tmp_path / "training.sqlite3") == after
    assert list(tmp_path.glob("before-mixture-*.sqlite3")) == backups


def test_failed_mixture_backup_retains_original_uniform_core(tmp_path, monkeypatch):
    game = make_game(CORE_GAME)
    checkpoint = tmp_path / "checkpoint.json"
    Trainer(
        game, algorithm="pluribus", pluribus={"average_mode": "sampled_uniform"}
    ).save_checkpoint(checkpoint)
    previous = open_trainer(game, tmp_path / "training.sqlite3", checkpoint=checkpoint)
    before = previous._store._document
    previous._store.close()

    def fail(*_):
        raise OSError("synthetic mixture backup failure")

    monkeypatch.setattr(TrainingStore, "backup", fail)
    with pytest.raises(OSError, match="mixture backup"):
        _open_core_trainer(tmp_path)
    assert header(tmp_path / "training.sqlite3") == before
