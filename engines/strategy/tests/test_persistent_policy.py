"""Policy queries use one committed iteration and never substitute current regrets."""

import json
import sqlite3
from random import Random

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_game_core.leduc import LeducPoker
from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy import persistent_policy
from texas_strategy.cfr import Trainer, _TableEntry
from texas_strategy.mushroom_abstraction import MushroomContextGame
from texas_strategy.policy import legal_information
from texas_strategy.training_store import TrainingStore, open_trainer


def decision(game):
    state, rng = game.initial_state(), Random(42)
    while game.actor(state) == -1:
        if callable(getattr(game, "sample_chance_outcome", None)):
            action, _ = game.sample_chance_outcome(state, rng)
        else:
            action, _ = game.chance_outcomes(state)[0]
        state = game.transition(state, action)
    return state


def save_entries(trainer, entries):
    trainer._store.begin(trainer.iterations)
    for key, entry in entries.items():
        trainer._store.set_entry(key, entry, 0)
    trainer._store.commit(trainer, trainer.iterations + 1)
    trainer.iterations += 1


def sample(tmp_path, game=None):
    game = game or KuhnPoker()
    state = decision(game)
    key, actions = legal_information(game, state)
    path = tmp_path / "policy.sqlite3"
    trainer = open_trainer(game, path)
    entry = _TableEntry(actions, [999.0] * len(actions), list(range(1, len(actions) + 1)))
    save_entries(trainer, {key: entry})
    return game, state, key, entry, path, trainer


@pytest.mark.parametrize("mushroom", [False, True])
def test_lookup_matches_trained_average_policy_for_leduc_and_eight_players(tmp_path, mushroom):
    game = (
        MushroomContextGame(MushroomSessionGame((48,) * 8, horizon=1)) if mushroom else LeducPoker()
    )
    trainer = Trainer(
        game,
        algorithm="pluribus",
        seed=42,
        pluribus={"strategy_interval": 10000 if mushroom else 1, "snapshot_interval": 1},
    )
    result = trainer.train(2 if mushroom else 10, max_nodes=100_000, deadline_seconds=20)
    assert result.completed_iterations == (2 if mushroom else 10)
    expected = trainer.average_policy(missing="error")
    assert expected.entries
    legacy, path = tmp_path / "legacy.json", tmp_path / "training.sqlite3"
    trainer.save_checkpoint(legacy)
    stored = open_trainer(game, path, checkpoint=legacy)
    stored._store.close()
    with persistent_policy.policy_snapshot(game, path) as policy:
        assert policy.iterations == trainer.iterations
        assert policy.metadata["num_players"] == (8 if mushroom else 2)
        for key in trainer._table:
            assert policy.lookup(*key) == expected.lookup(*key)


def test_snapshot_keeps_old_rows_metadata_and_missing_keys_after_concurrent_commit(tmp_path):
    game, state, key, entry, path, trainer = sample(tmp_path)
    try:
        with persistent_policy.policy_snapshot(game, path) as old:
            before = old.lookup(*key)
            digest = old.metadata_digest
            assert old.contains(game, state)
            changed = _TableEntry(entry.actions, [0] * len(entry.actions), [9, 1])
            save_entries(trainer, {key: changed, (1, "later"): changed})
            assert old.iterations == 1 and old.metadata_digest == digest
            assert old.lookup(*key) == before
            assert old.lookup(1, "later") is None
            with persistent_policy.policy_snapshot(game, path) as current:
                assert current.iterations == 2
                assert current.metadata_digest != digest
                assert current.lookup(*key).probabilities == pytest.approx((0.9, 0.1))
                assert current.lookup(1, "later") is not None
        with pytest.raises(ValueError, match="closed"):
            old.lookup(*key)
    finally:
        trainer._store.close()


def test_zero_average_is_missing_and_uniform_is_explicit_counted_fallback(tmp_path):
    game, state, key, entry, path, trainer = sample(tmp_path)
    save_entries(
        trainer,
        {
            key: _TableEntry(entry.actions, [100, 200], [0, 0]),
        },
    )
    trainer._store.close()
    with persistent_policy.policy_snapshot(game, path) as policy:
        assert policy.lookup(*key) is None and not policy.contains(game, state)
        with pytest.raises(KeyError, match="does not cover"):
            policy.probabilities(game, state)
        assert policy.fallback_count == 1 and policy.covered_queries == 0
    with persistent_policy.policy_snapshot(game, path, missing="uniform") as policy:
        assert policy.probabilities(game, state) == (0.5, 0.5)
        assert not policy.contains(game, state)
        assert policy.fallback_count == 1 and policy.covered_queries == 0


@pytest.mark.parametrize(
    "regrets,sums,expected",
    [
        ([0, 0], [1, 1], True),
        ([-1, 0], [1, 1], False),
        ([0, 0], [2, 1], False),
        ([1, -1], [0, 0], True),
    ],
)
def test_uniform_without_regrets_check_is_conservative_not_a_quality_assessment(
    tmp_path, regrets, sums, expected
):
    game, _state, key, entry, path, trainer = sample(tmp_path)
    save_entries(trainer, {key: _TableEntry(entry.actions, regrets, sums)})
    trainer._store.close()
    with persistent_policy.policy_snapshot(game, path) as policy:
        assert policy.is_uniform_without_regrets(*key) is expected
        assert policy.is_uniform_without_regrets(0, "not-trained") is True


def test_payoff_evidence_uses_the_same_snapshot_and_releases_it(tmp_path):
    game, _state, key, entry, path, trainer = sample(tmp_path)
    try:
        with persistent_policy.policy_snapshot(game, path) as policy:
            assert not policy.is_uniform_without_regrets(*key)
            save_entries(trainer, {key: _TableEntry(entry.actions, [0, 0], [1, 1])})
            assert not policy.is_uniform_without_regrets(*key)
            with persistent_policy.policy_snapshot(game, path) as current:
                assert current.is_uniform_without_regrets(*key)
        with pytest.raises(ValueError, match="closed"):
            policy.is_uniform_without_regrets(*key)
    finally:
        trainer._store.close()


@pytest.mark.parametrize("value", ["[NaN,2]", "[1e999,2]", "[true,2]", "[1]", "{}"])
def test_invalid_payoff_evidence_cannot_be_accepted_due_to_nonuniform_averages(tmp_path, value):
    game, _state, key, _entry, path, trainer = sample(tmp_path)
    trainer._store.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE training_entries SET regrets=?", (value,))
    with persistent_policy.policy_snapshot(game, path) as policy:
        with pytest.raises(ValueError):
            policy.is_uniform_without_regrets(*key)


def test_rules_action_order_and_game_binding_are_checked(tmp_path):
    game, state, key, _entry, path, trainer = sample(tmp_path)
    trainer._store.close()
    with pytest.raises(ValueError, match="incompatible"):
        with persistent_policy.policy_snapshot(LeducPoker(), path):
            pytest.fail("Wrong game was accepted")

    class Reordered:
        def __getattr__(self, name):
            return getattr(game, name)

        def legal_actions(self, state):
            return tuple(reversed(game.legal_actions(state)))

    with persistent_policy.policy_snapshot(game, path) as policy:
        assert policy.contains(game, state)
        assert policy.probabilities(game, state) == policy.lookup(*key).probabilities
        assert policy.covered_queries == 1
        with pytest.raises(ValueError, match="action order"):
            policy.contains(Reordered(), state)
        with pytest.raises(ValueError, match="action order"):
            policy.probabilities(Reordered(), state)
        with pytest.raises(ValueError, match="do not match"):
            policy.contains(KuhnPoker(num_players=3), state)
        metadata = policy.metadata
        metadata["num_players"] = 999
        metadata["pluribus"]["strategy_interval"] = 999
        assert policy.metadata["num_players"] == 2
        assert policy.metadata["pluribus"]["strategy_interval"] == 1


def test_mushroom_policy_reuses_allowed_stack_and_button_context_without_relaxing_restore(tmp_path):
    first = MushroomContextGame(MushroomSessionGame((800,) * 8, horizon=1))
    game, state, _key, _entry, path, trainer = sample(tmp_path, first)
    trainer._store.close()
    shifted = MushroomContextGame(MushroomSessionGame((801,) * 8, button=3, horizon=1))
    other = decision(shifted)
    assert game.checkpoint_context != shifted.checkpoint_context
    assert game.rules_version == shifted.rules_version
    with pytest.raises(ValueError, match="context"):
        open_trainer(shifted, path)
    with persistent_policy.policy_snapshot(game, path) as original:
        expected = original.probabilities(game, state)
    with persistent_policy.policy_snapshot(shifted, path) as policy:
        assert policy.contains(shifted, other)
        assert policy.probabilities(shifted, other) == expected
        assert first.concrete_action(state, "all_in") != shifted.concrete_action(other, "all_in")


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE training_entries SET strategy_sum='[-1,2]'",
        "UPDATE training_entries SET strategy_sum='[NaN,2]'",
        "UPDATE training_entries SET strategy_sum='[1e999,2]'",
        "UPDATE training_entries SET strategy_sum='[" + "9" * 400 + ",2]'",
        "UPDATE training_entries SET strategy_sum='[true,2]'",
        "UPDATE training_entries SET strategy_sum='[1]'",
        'UPDATE training_entries SET actions=\'["check","check"]\'',
        "UPDATE training_entries SET actions='{}'",
        "UPDATE training_entries SET has_average=0",
        "UPDATE training_entries SET stage=1",
    ],
)
def test_requested_corrupt_row_is_rejected_instead_of_falling_back(tmp_path, sql):
    game, state, _key, _entry, path, trainer = sample(tmp_path)
    trainer._store.close()
    with sqlite3.connect(path) as db:
        db.execute(sql)
    with persistent_policy.policy_snapshot(game, path, missing="uniform") as policy:
        with pytest.raises(ValueError):
            policy.probabilities(game, state)
        assert policy.fallback_count == 0


def test_normalization_handles_large_finite_values_without_overflow(tmp_path):
    game, state, key, entry, path, trainer = sample(tmp_path)
    save_entries(trainer, {key: _TableEntry(entry.actions, [0, 0], [1e308, 1e308])})
    trainer._store.close()
    with persistent_policy.policy_snapshot(game, path) as policy:
        assert policy.probabilities(game, state) == (0.5, 0.5)


def test_queries_are_readonly_indexed_and_never_scan_or_load_regrets(tmp_path, monkeypatch):
    game, state, key, entry, path, trainer = sample(tmp_path)
    save_entries(trainer, {(0, f"unrelated-{i}"): entry for i in range(1000)})
    trainer._store.close()
    statements = []
    connect = sqlite3.connect

    def connection(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    def forbidden(*args, **kwargs):
        pytest.fail("Policy query must not scan the training table")

    monkeypatch.setattr(persistent_policy.sqlite3, "connect", connection)
    monkeypatch.setattr(TrainingStore, "scan", forbidden)
    with persistent_policy.policy_snapshot(game, path) as policy:
        assert policy.lookup(*key) is not None
        assert policy.contains(game, state)
        assert policy.probabilities(game, state)
        assert policy._db.execute("PRAGMA cache_size").fetchone()[0] == -8192
        assert policy._db.execute("PRAGMA mmap_size").fetchone()[0] == 0
        assert policy._db.execute("PRAGMA temp_store").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            policy._db.execute("UPDATE training_meta SET singleton=1")
    reads = [s for s in statements if s.upper().startswith("SELECT") and "training_entries" in s]
    assert len(reads) == 3
    assert all("WHERE player=" in s and "AND information_key=" in s for s in reads)
    assert all("regrets" not in s and "COUNT" not in s for s in reads)
    assert not any("quick_check" in s for s in statements)


def test_exception_releases_reader_and_missing_database_is_not_created(tmp_path):
    game, _state, key, _entry, path, trainer = sample(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="consumer failed"):
            with persistent_policy.policy_snapshot(game, path) as policy:
                assert policy.lookup(*key)
                connection = policy._db
                raise RuntimeError("consumer failed")
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
        assert trainer._store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        with pytest.raises(ValueError, match="database"):
            with persistent_policy.policy_snapshot(game, tmp_path / "missing"):
                pytest.fail("Missing file was accepted")
        assert not (tmp_path / "missing").exists()
    finally:
        trainer._store.close()


def test_bad_header_releases_connection_before_yield(tmp_path, monkeypatch):
    game, _state, _key, _entry, path, trainer = sample(tmp_path)
    trainer._store.close()
    with sqlite3.connect(path) as db:
        document = json.loads(db.execute("SELECT document FROM training_meta").fetchone()[0])
        document["metadata"]["rules_version"] = "incompatible"
        db.execute("UPDATE training_meta SET document=?", (json.dumps(document),))
    connections = []
    connect = sqlite3.connect

    def tracked(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(persistent_policy.sqlite3, "connect", tracked)
    with pytest.raises(ValueError, match="incompatible"):
        with persistent_policy.policy_snapshot(game, path):
            pytest.fail("Invalid header was accepted")
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
