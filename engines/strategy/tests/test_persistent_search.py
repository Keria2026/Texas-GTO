"""Search consumes one real SQLite blueprint snapshot, including its strict gaps.

These tiny, fully enumerated research games exercise the integration. They do
not certify a live WPK range, eight-player coverage, or strategy strength.
"""

import json
import sqlite3
from contextlib import closing

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_game_core.leduc import LeducPoker
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import CancellationError
from texas_strategy.persistent_policy import policy_snapshot
from texas_strategy.policy import legal_information
from texas_strategy.search import SearchCoverageError, solve_subgame
from texas_strategy.training_store import open_trainer


def trained_model(tmp_path, game, iterations=100):
    trainer = Trainer(game, algorithm="pluribus", seed=42, pluribus={"snapshot_interval": 1})
    assert trainer.train(iterations).completed_iterations == iterations
    expected = trainer.average_policy(missing="error")
    legacy, path = tmp_path / "legacy.json", tmp_path / "training.sqlite3"
    trainer.save_checkpoint(legacy)
    return path, open_trainer(game, path, checkpoint=legacy), expected


def assert_released(policy, connection, path):
    with pytest.raises(ValueError, match="closed"):
        policy.lookup(0, "closed-request")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    # A leaked WAL reader would prevent truncating commits made after its snapshot.
    with closing(sqlite3.connect(path, timeout=0)) as check:
        assert check.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone() == (0, 0, 0)


def assert_same_search(actual, expected):
    assert actual.actions == expected.actions
    assert actual.probabilities == expected.probabilities
    assert actual.root_distribution == expected.root_distribution
    assert actual.policy.entries == expected.policy.entries
    assert actual.average_policy.entries == expected.average_policy.entries
    assert actual.diagnostics.completed_iterations == expected.diagnostics.completed_iterations
    assert actual.subgame.rollout_budget.nodes == expected.subgame.rollout_budget.nodes
    assert actual.blueprint_fallback_queries == 0
    assert actual.quality == "conditional_approximate_search"


@pytest.mark.parametrize("kind", ["kuhn", "leduc"])
def test_search_root_and_tails_keep_old_policy_during_training_commit(tmp_path, monkeypatch, kind):
    game = KuhnPoker() if kind == "kuhn" else LeducPoker()
    target = (
        game.replay((2, 0), ("check",))
        if kind == "kuhn"
        else game.replay((4, 0), ("check", "check", "board:2"))
    )
    path, writer, before = trained_model(tmp_path, game, 100 if kind == "kuhn" else 1000)
    settings = {"iterations": 3, "depth_limit": 1, "seed": 17}
    expected = solve_subgame(game, target, before, **settings)
    assert expected.subgame.rollout_budget.nodes > 0
    committed, reads_after_commit = False, 0
    iteration = writer.iterations
    try:
        with policy_snapshot(game, path) as policy:
            connection, digest = policy._db, policy.metadata_digest
            original = policy.probabilities

            def interleaved_query(query_game, state):
                nonlocal committed, reads_after_commit
                probabilities = original(query_game, state)
                assert probabilities == before.probabilities(query_game, state)
                assert policy.iterations == iteration and policy.metadata_digest == digest
                if not committed:
                    # A separate writable connection commits while the reader remains open.
                    assert writer.train(3).completed_iterations == 3
                    committed = True
                else:
                    reads_after_commit += 1
                return probabilities

            monkeypatch.setattr(policy, "probabilities", interleaved_query)
            result = solve_subgame(game, target, policy, **settings)
            assert committed and reads_after_commit > 1
            assert_same_search(result, expected)
            assert policy.fallback_count == 0
            after = writer.average_policy(missing="error")
            changed = [key for key in before.entries if before.lookup(*key) != after.lookup(*key)]
            assert changed, "The concurrent training must actually change policy rows"
            with policy_snapshot(game, path) as current:
                assert current.iterations == iteration + 3
                assert current.metadata_digest != digest
                for key in changed:
                    assert policy.lookup(*key) == before.lookup(*key)
                    assert current.lookup(*key) == after.lookup(*key)
        assert_released(policy, connection, path)
    finally:
        writer._store.close()


def test_sampled_search_matches_memory_blueprint_without_uniform_fallback(tmp_path):
    game = KuhnPoker()
    target = game.replay((2, 0))
    path, writer, before = trained_model(tmp_path, game)
    try:
        settings = {
            "iterations": 20,
            "depth_limit": 1,
            "algorithm": "mccfr",
            "sampled_rollouts": True,
            "seed": 23,
        }
        expected = solve_subgame(game, target, before, **settings)
        with policy_snapshot(game, path) as policy:
            connection = policy._db
            result = solve_subgame(game, target, policy, **settings)
            assert_same_search(result, expected)
            assert result.subgame.rollout_samples > 0
            assert result.subgame.rollout_cache_entries == 0
            assert policy.covered_queries > 0 and policy.fallback_count == 0
        assert_released(policy, connection, path)
    finally:
        writer._store.close()


@pytest.mark.parametrize("phase", ["root", "tail"])
@pytest.mark.parametrize("gap", ["absent", "zero_average"])
def test_missing_coverage_rejects_during_root_or_tail_and_closes_snapshot(tmp_path, phase, gap):
    game = KuhnPoker()
    path, writer, _ = trained_model(tmp_path, game)
    writer._store.close()
    target = game.replay((2, 0), ("check",) if phase == "root" else ())
    missing_state = game.replay((0, 1), () if phase == "root" else ("check",))
    key, actions = legal_information(game, missing_state)
    with closing(sqlite3.connect(path)) as edit:
        if gap == "absent":
            edit.execute("DELETE FROM training_entries WHERE player=? AND information_key=?", key)
        else:
            # Positive current regrets are not an accumulated average policy.
            edit.execute(
                "UPDATE training_entries SET strategy_sum=?,regrets=?,has_average=0 "
                "WHERE player=? AND information_key=?",
                (json.dumps([0] * len(actions)), json.dumps([999] * len(actions)), *key),
            )
        edit.commit()
    with pytest.raises(SearchCoverageError, match="blueprint does not cover"):
        # Even this explicitly lenient policy must obey search's default strict coverage.
        with policy_snapshot(game, path, missing="uniform") as policy:
            connection = policy._db
            assert policy.lookup(*key) is None
            solve_subgame(game, target, policy, iterations=2, depth_limit=1)
    assert policy.fallback_count == 0
    assert_released(policy, connection, path)


def test_cancellation_after_real_policy_reads_releases_reader_and_allows_next_search(tmp_path):
    game = KuhnPoker()
    target = game.replay((2, 0))
    path, writer, before = trained_model(tmp_path, game)
    try:
        with pytest.raises(CancellationError):
            with policy_snapshot(game, path) as policy:
                connection = policy._db
                solve_subgame(
                    game,
                    target,
                    policy,
                    iterations=20,
                    depth_limit=1,
                    should_cancel=lambda: policy.covered_queries >= 2,
                )
        assert policy.covered_queries >= 2 and policy.fallback_count == 0
        assert writer.average_policy(missing="error").entries == before.entries
        assert writer.train(1).completed_iterations == 1
        assert_released(policy, connection, path)
        with policy_snapshot(game, path) as fresh:
            result = solve_subgame(game, target, fresh, iterations=2, depth_limit=1)
            assert fresh.iterations == 101
            assert result.diagnostics.completed_iterations == 2
            assert result.blueprint_fallback_queries == 0
    finally:
        writer._store.close()
