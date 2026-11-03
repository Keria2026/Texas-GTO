"""Synthetic causal capture-to-curriculum tests; no strategy-strength claim."""

import json
import sqlite3
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_capture_recommendation import ORIGIN, batch, eight_player_events
from test_core_training import wait_for

from texas_analysis import capture_openings
from texas_analysis.app import create_app
from texas_analysis.core_training import _incorporate_openings
from texas_analysis.network_stream import NetworkStore
from texas_analysis.portable_state import export_state, restore_state
from texas_strategy.evaluation import CancellationError
from texas_strategy.mushroom_population import MushroomPopulationGame
from texas_strategy.training_store import open_trainer


def events(source="table-feed", *, extra=4, cards=("As", "Kd")):
    result = eight_player_events(ante=4)
    for event in result:
        event["source_id"] = source
    result[-1]["data"]["players"][0]["stack"] += extra
    result[-1]["data"]["hero_cards"] = list(cards)
    return result


def registry(store):
    with store.connect() as db:
        return capture_openings.load(db)


def renumber(rows):
    for i, row in enumerate(rows, 1):
        row["sequence"] = str(i)
    return rows


def test_public_context_is_exact_idempotent_and_does_not_store_known_cards(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    first = events()
    store.append(ORIGIN, batch(first))
    store.collect_training_openings()
    roots, limited = registry(store)
    assert not limited and roots == [
        {
            "button": 0,
            "weight": 1,
            "stacks": [804] + [800] * 7,
            "outstanding": [0] * 8,
            "waiting_for_top_up": [],
        }
    ]
    second = events("other-feed", cards=("Qc", "Jh"))
    store.append("chrome-extension://" + "b" * 32, batch(second))
    store.collect_training_openings()
    store.append(ORIGIN, batch(first))
    store.collect_training_openings()
    assert registry(store) == (roots, False)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM capture_training_openings").fetchone()[0] == 2
        assert db.execute("SELECT through_row FROM capture_training_opening_progress").fetchone()[
            0
        ] == len(first) + len(second)


def test_future_or_other_origin_settlements_never_repair_a_past_opening(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    original = events()
    incomplete = renumber(deepcopy(original[1:]))
    store.append(ORIGIN, batch(incomplete))
    store.append("chrome-extension://" + "b" * 32, batch([original[0]]))
    store.collect_training_openings()
    assert registry(store)[0] == []
    delayed = deepcopy(original[0])
    delayed["sequence"] = str(len(incomplete) + 1)
    store.append(ORIGIN, batch([delayed]))
    store.collect_training_openings()
    assert registry(store)[0] == []
    current = deepcopy(incomplete[-1])
    current["sequence"] = str(len(incomplete) + 2)
    store.append(ORIGIN, batch([current]))
    store.collect_training_openings()
    assert len(registry(store)[0]) == 1


def test_same_hand_restart_after_gap_cannot_create_a_trusted_training_root(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    rows = events()
    gap = {**deepcopy(rows[1]), "kind": "capture_gap", "data": {"reason": "synthetic gap"}}
    rows = renumber(rows[:2] + [gap, deepcopy(rows[1])] + rows[2:])
    store.append(ORIGIN, batch(rows))
    store.collect_training_openings()
    assert registry(store)[0] == []
    with store.connect() as db:
        rejected = json.loads(
            db.execute("SELECT rejected FROM capture_training_opening_progress").fetchone()[0]
        )
    assert rejected == {"incomplete_action_history": 1}


def test_bounded_backfill_capacity_and_cancellation_are_explicit(tmp_path, monkeypatch):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    store.append(ORIGIN, batch(events("first")))
    store.append(ORIGIN, batch(events("second", extra=8)))
    with pytest.raises(CancellationError):
        store.collect_training_openings(lambda: True)
    assert registry(store)[0] == []
    monkeypatch.setattr(capture_openings, "MAX_BATCH", 1)
    monkeypatch.setattr(capture_openings, "MAX_OPENINGS", 1)
    store.collect_training_openings()
    roots, limited = registry(store)
    assert len(roots) == 1 and not limited
    store.collect_training_openings()
    assert registry(store) == (roots, True)
    with store.connect() as db:
        rejected = json.loads(
            db.execute("SELECT rejected FROM capture_training_opening_progress").fetchone()[0]
        )
    assert rejected == {"opening_capacity": 1}


def test_captured_opening_updates_one_core_and_backs_up_only_the_first_change(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    store.append(ORIGIN, batch(events()))
    store.collect_training_openings()
    directory = tmp_path / "core"
    original = MushroomPopulationGame(
        [
            {
                "weight": 100,
                "stacks": [64] * 8,
                "outstanding": [0] * 8,
                "waiting_for_top_up": [],
                "button": 0,
            }
        ],
        ante=4,
        horizon=1,
    )
    trainer = open_trainer(original, directory / "training.sqlite3")
    before = deepcopy(trainer._store._document)
    try:
        assert not _incorporate_openings(trainer, directory)
        assert trainer.game.observed_opening_count == 1
        assert trainer.game.roots[1]["stacks"] == [804] + [800] * 7
        assert {
            **trainer._store._document,
            "training_context": before["training_context"],
        } == before
        assert len(list(directory.glob("before-openings-*.sqlite3"))) == 1
        context = trainer.game.checkpoint_context
        _incorporate_openings(trainer, directory)
        assert trainer.game.checkpoint_context == context
        store.append(ORIGIN, batch(events("next", extra=8)))
        store.collect_training_openings()
        _incorporate_openings(trainer, directory)
        assert trainer.game.observed_opening_count == 2
        assert trainer.game.opening_updates["steps"] == [
            {"iterations": 0, "count": 1},
            {"iterations": 0, "count": 2},
        ]
        assert len(list(directory.glob("before-openings-*.sqlite3"))) == 1
    finally:
        trainer._store.close()


def test_failed_opening_backup_never_changes_the_committed_core(tmp_path, monkeypatch):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    store.append(ORIGIN, batch(events()))
    store.collect_training_openings()
    directory = tmp_path / "core"
    trainer = open_trainer(MushroomPopulationGame.default(ante=4), directory / "training.sqlite3")
    before = deepcopy(trainer._store._document)

    def fail(_):
        raise OSError("synthetic backup failure")

    monkeypatch.setattr(trainer._store, "backup", fail)
    try:
        with pytest.raises(OSError, match="synthetic"):
            _incorporate_openings(trainer, directory)
        assert trainer._store._document == before
        assert trainer.game.observed_opening_count == 0
    finally:
        trainer._store.close()


def test_invalid_persisted_opening_cannot_change_a_core(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    store.append(ORIGIN, batch(events()))
    store.collect_training_openings()
    with store.connect() as db:
        db.execute("UPDATE capture_training_openings SET root=NULL")
    directory = tmp_path / "core"
    trainer = open_trainer(MushroomPopulationGame.default(ante=4), directory / "training.sqlite3")
    before = deepcopy(trainer._store._document)
    try:
        with pytest.raises(ValueError, match="digest"):
            _incorporate_openings(trainer, directory)
        assert trainer._store._document == before
        assert trainer.game.observed_opening_count == 0
        assert not list(directory.glob("before-openings-*.sqlite3"))
    finally:
        trainer._store.close()


def test_service_collects_and_trains_automatically_then_exports_both_histories(tmp_path):
    data = tmp_path / "data"
    store = NetworkStore(data / "capture.sqlite3", stream="capture")
    store.append(ORIGIN, batch(events()))
    old = MushroomPopulationGame(
        [
            {
                "weight": 100,
                "stacks": [64] * 8,
                "outstanding": [0] * 8,
                "waiting_for_top_up": [],
                "button": 0,
            }
        ],
        ante=4,
        horizon=2,
    )
    trainer = open_trainer(old, data / "core/training.sqlite3")
    trainer._store.close()
    app = create_app(data_dir=data, auto_train=True)
    core = app.state.core_training
    core.idle_seconds = 0
    with TestClient(app):
        saved = wait_for(
            core, lambda s: s.observed_openings == 1 and s.iterations >= 1, seconds=180
        )
        assert saved.training_roots == 2 and not saved.opening_capacity_reached
        assert not saved.recommendation_ready
    with store.connect() as db:
        roots = capture_openings.load(db)
        cursor = db.execute("SELECT through_row FROM capture_training_opening_progress").fetchone()[
            0
        ]
    assert len(roots[0]) == 1
    export_state(data, tmp_path / "bundle")
    restore_state(tmp_path / "bundle", tmp_path / "restored")
    restored = NetworkStore(tmp_path / "restored/capture.sqlite3", stream="capture")
    restored.collect_training_openings()
    with restored.connect() as db:
        assert capture_openings.load(db) == roots
        assert (
            db.execute("SELECT through_row FROM capture_training_opening_progress").fetchone()[0]
            == cursor
        )
    with (
        sqlite3.connect(data / "core/training.sqlite3") as original_db,
        sqlite3.connect(tmp_path / "restored/core/training.sqlite3") as restored_db,
    ):
        original_header = original_db.execute("SELECT document FROM training_meta").fetchone()[0]
        restored_header = restored_db.execute("SELECT document FROM training_meta").fetchone()[0]
    assert original_header == restored_header
    game = MushroomPopulationGame.from_context(json.loads(restored_header)["training_context"])
    trainer = open_trainer(game, tmp_path / "restored/core/training.sqlite3")
    try:
        assert trainer.game.observed_opening_count == 1 and trainer.iterations >= 1
    finally:
        trainer._store.close()
