"""Disk-backed training must retain the exact existing Pluribus update sequence."""

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_game_core.leduc import LeducPoker
from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy import training_store
from texas_strategy.cfr import Trainer
from texas_strategy.mushroom_abstraction import MushroomContextGame
from texas_strategy.training_store import open_trainer


@pytest.fixture(autouse=True)
def force_iteration_spills(monkeypatch):
    monkeypatch.setattr(training_store, "_WORKING_CACHE_SIZE", 2)


def assert_same(actual, expected):
    assert actual.iterations == expected.iterations
    assert dict(actual._table.items()) == dict(expected._table.items())
    assert dict(actual._betting_rounds.items()) == dict(expected._betting_rounds.items())
    assert actual._rng.getstate() == expected._rng.getstate()


@pytest.mark.parametrize("game", [KuhnPoker(), LeducPoker()])
@pytest.mark.parametrize("average_mode", ["enumerated", "sampled_uniform"])
def test_disk_updates_discount_snapshots_pruning_and_restart_match_memory(
    tmp_path, game, average_mode
):
    direct = Trainer(
        game,
        algorithm="pluribus",
        seed=42,
        pluribus={
            "average_mode": average_mode,
            "discount_interval": 2,
            "discount_until": 18,
            "snapshot_interval": 3,
            "prune_after": 3,
            "prune_threshold": -2.0,
            "regret_floor": -10.0,
        },
    )
    legacy = tmp_path / "legacy.json"
    direct.train(4)
    direct.save_checkpoint(legacy)
    database = tmp_path / "training.sqlite3"
    disk = open_trainer(game, database, checkpoint=legacy)
    try:
        for _ in range(4):
            left, right = disk.train(3), direct.train(3)
            assert left.nodes == right.nodes
            assert left.pruned_actions == right.pruned_actions
            assert left.snapshot_iterations == right.snapshot_iterations
            assert_same(disk, direct)
            disk._store.close()
            disk = open_trainer(game, database)
            assert_same(disk, direct)
    finally:
        disk._store.close()


def test_cancellation_during_global_transform_rolls_back_all_rows_and_rng(tmp_path, monkeypatch):
    game = LeducPoker()
    direct = Trainer(game, algorithm="pluribus", seed=42, pluribus={"snapshot_interval": 1})
    direct.train(6)
    legacy = tmp_path / "legacy.json"
    direct.save_checkpoint(legacy)
    database = tmp_path / "training.sqlite3"
    disk = open_trainer(game, database, checkpoint=legacy)
    scanning = False
    visited = 0
    original = disk._store.scan

    def scan():
        nonlocal scanning, visited
        scanning = True
        for row in original():
            visited += 1
            yield row

    monkeypatch.setattr(disk._store, "scan", scan)
    try:
        result = disk.train(1, should_cancel=lambda: scanning and visited > 10)
        assert visited > 10
        assert result.stop_reason == "cancelled" and result.completed_iterations == 0
        assert_same(disk, direct)
    finally:
        disk._store.close()
    disk = open_trainer(game, database)
    try:
        assert_same(disk, direct)
        disk.train(1)
        direct.train(1)
        assert_same(disk, direct)
    finally:
        disk._store.close()


def test_failed_persistence_leaves_a_resumable_completed_iteration(tmp_path, monkeypatch):
    game = KuhnPoker()
    disk = open_trainer(game, tmp_path / "training.sqlite3")
    direct = Trainer(game, algorithm="pluribus", seed=42)
    disk.train(3)
    direct.train(3)
    original = disk._store.set_entry
    writes = 0

    def fail_after_write(key, entry, stage):
        nonlocal writes
        original(key, entry, stage)
        writes += 1
        if writes == 2:
            raise OSError("injected storage failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(disk._store, "set_entry", fail_after_write)
            with pytest.raises(OSError, match="injected storage failure"):
                disk.train(1)
        assert_same(disk, direct)
        disk.train(1)
        direct.train(1)
        assert_same(disk, direct)
    finally:
        disk._store.close()


@pytest.mark.parametrize("failure", ["cancelled", "write_error"])
def test_failed_iteration_discards_spilled_updates_and_retries_exactly(
    tmp_path, monkeypatch, failure
):
    game = LeducPoker()
    disk = open_trainer(game, tmp_path / "training.sqlite3")
    direct = Trainer(game, algorithm="pluribus", seed=42)
    original = training_store._IterationStore._save
    writes = 0

    def save(self, key, entry, stage):
        nonlocal writes
        original(self, key, entry, stage)
        writes += 1
        if writes == 5 and failure == "write_error":
            raise OSError("injected scratch write failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(training_store._IterationStore, "_save", save)
            if failure == "write_error":
                with pytest.raises(OSError, match="injected scratch write failure"):
                    disk.train(1)
            else:
                result = disk.train(1, should_cancel=lambda: writes >= 5)
                assert result.stop_reason == "cancelled" and result.completed_iterations == 0
            assert writes >= 5
        assert_same(disk, direct)
        assert not disk._store._db.in_transaction
        disk.train(2)
        direct.train(2)
        assert_same(disk, direct)
    finally:
        disk._store.close()


def test_spills_keep_active_entry_identity_and_zero_entry_validation(tmp_path):
    disk = open_trainer(LeducPoker(), tmp_path / "training.sqlite3")
    working = training_store._IterationStore(disk._store)
    key, actions = (0, "pinned"), ("fold", "call")
    try:
        with working.entry(key, actions, 0) as parent:
            parent.regrets[0] = 7
            for i in range(30):
                with working.entry((1, f"zero-{i}"), actions, 0):
                    pass
                assert len(working._cache) <= 2
            with working.entry(key, actions, 0) as child:
                assert child is parent
                child.regrets[1] = -3
            assert parent.regrets == [7, -3]
        for i in range(30, 35):
            with working.entry((1, f"zero-{i}"), actions, 0):
                pass
        with working.entry(key, actions, 0) as reloaded:
            assert reloaded.regrets == [7, -3]
        assert (1, "zero-0") in working
        with pytest.raises(ValueError, match="legal actions or betting round"):
            with working.entry((1, "zero-0"), actions, 1):
                pass
        assert len(disk._store) == 0
    finally:
        working.close()
        assert not working._cache and not working._active
        disk._store.close()


def test_eight_player_mushroom_persistent_training_keeps_exact_rewards_and_rng(tmp_path):
    game = MushroomContextGame(MushroomSessionGame(horizon=2))
    direct = Trainer(
        game,
        algorithm="pluribus",
        seed=42,
        pluribus={"strategy_interval": 10000, "snapshot_interval": 1},
    )
    legacy = tmp_path / "legacy.json"
    direct.save_checkpoint(legacy)
    disk = open_trainer(game, tmp_path / "training.sqlite3", checkpoint=legacy)
    try:
        assert disk.train(2, max_nodes=500_000).completed_iterations == 2
        assert direct.train(2, max_nodes=500_000).completed_iterations == 2
        assert_same(disk, direct)
    finally:
        disk._store.close()
