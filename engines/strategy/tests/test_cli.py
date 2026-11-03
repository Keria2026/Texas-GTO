import json

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer
from texas_strategy.cli import main


def test_checkpoint_backup_exports_sqlite_without_building_a_game_or_blueprint(
    tmp_path, capsys, monkeypatch
):
    from texas_strategy.training_store import open_trainer

    source, target = tmp_path / "training.sqlite3", tmp_path / "portable.sqlite3"
    trainer = open_trainer(KuhnPoker(), source)
    try:
        trainer.train(2)
        expected = dict(trainer._table.items())
        rng = trainer._rng.getstate()
    finally:
        trainer._store.close()

    def unexpected_game(_args):
        pytest.fail("SQLite backup must not build a game or materialize a blueprint")

    monkeypatch.setattr("texas_strategy.cli._game", unexpected_game)
    assert main(["checkpoint-backup", "--source", str(source), "--output", str(target)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "command": "checkpoint-backup",
        "checkpoint": str(target),
        "checkpoint_bytes": target.stat().st_size,
    }
    restored = open_trainer(KuhnPoker(), target)
    try:
        assert restored.iterations == 2
        assert dict(restored._table.items()) == expected
        assert restored._rng.getstate() == rng
    finally:
        restored._store.close()


def test_checkpoint_backup_reports_invalid_sources_and_never_overwrites(tmp_path, capsys):
    from texas_strategy.training_store import open_trainer

    source, target = tmp_path / "training.sqlite3", tmp_path / "existing.sqlite3"
    trainer = open_trainer(KuhnPoker(), source)
    trainer._store.close()
    target.write_bytes(b"preserved target")
    for output in (source, target):
        before = output.read_bytes()
        assert main(["checkpoint-backup", "--source", str(source), "--output", str(output)]) == 2
        report = json.loads(capsys.readouterr().err)
        assert report["command"] == "checkpoint-backup" and report["error"]
        assert output.read_bytes() == before
    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_bytes(b"not a SQLite checkpoint")
    for path in (invalid, tmp_path / "missing.sqlite3"):
        assert (
            main(
                [
                    "checkpoint-backup",
                    "--source",
                    str(path),
                    "--output",
                    str(tmp_path / "new.sqlite3"),
                ]
            )
            == 2
        )
        assert json.loads(capsys.readouterr().err)["command"] == "checkpoint-backup"
        assert not (tmp_path / "new.sqlite3").exists()


def test_eight_player_straddle_cli_trains_exports_and_resumes(tmp_path, capsys):
    path, checkpoint = tmp_path / "eight.json", tmp_path / "eight-checkpoint.json"
    config = [
        "--game",
        "holdem",
        "--players",
        "8",
        "--stacks",
        "8,8,8,8,8,8,8,8",
        "--small-blind",
        "2",
        "--big-blind",
        "4",
        "--straddle",
        "8",
        "--bet-fractions",
        "none",
        "--iterations",
        "1",
        "--blueprint",
        str(path),
    ]
    assert main(["train", *config, "--algorithm", "pluribus", "--checkpoint", str(checkpoint)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["training"]["completed_iterations"] == 1
    assert main(["resume", *config, "--from-checkpoint", str(checkpoint)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["metadata"]["iterations"] == 2
    assert report["metadata"]["num_players"] == 8
    assert main(["train", "--straddle", "8", "--blueprint", str(path)]) == 2
    assert "only for Hold'em" in json.loads(capsys.readouterr().err)["error"]


@pytest.mark.parametrize("abstraction", ["none", "mushroom_context"])
def test_mushroom_cli_runs_the_session_model_and_preserves_the_horizon_in_artifacts(
    tmp_path, capsys, abstraction
):
    path, checkpoint = tmp_path / "mushroom.json", tmp_path / "mushroom-checkpoint.json"
    config = [
        "--game",
        "mushroom",
        "--abstraction",
        abstraction,
        "--players",
        "4",
        "--stacks",
        "48,48,48,48",
        "--small-blind",
        "2",
        "--big-blind",
        "4",
        "--straddle",
        "8",
        "--session-hands",
        "1",
        "--iterations",
        "1",
        "--blueprint",
        str(path),
    ]
    assert main(["train", *config, "--algorithm", "pluribus", "--checkpoint", str(checkpoint)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["training"]["completed_iterations"] == 1
    assert report["metadata"]["game_id"] == "mushroom"
    assert report["metadata"]["pluribus"]["strategy_interval"] == 1
    assert report["metadata"]["pluribus"]["average_mode"] == "sampled_mixture"
    assert report["metadata"]["pluribus"]["average_samples"] == 16
    assert any("horizon" in warning for warning in report["warnings"])
    assert main(["resume", *config, "--from-checkpoint", str(checkpoint)]) == 0
    assert json.loads(capsys.readouterr().out)["metadata"]["iterations"] == 2
    invalid = config.copy()
    invalid[invalid.index("--session-hands") + 1] = "0"
    assert main(["train", *invalid, "--algorithm", "pluribus"]) == 2
    assert "horizon" in json.loads(capsys.readouterr().err)["error"]


@pytest.mark.parametrize("algorithm", ["mccfr", "pluribus"])
def test_train_resume_matches_uninterrupted_checkpoint_and_writes_report(
    tmp_path, capsys, algorithm
):
    blueprint = tmp_path / "blueprint.json"
    checkpoint = tmp_path / "checkpoint.json"
    report = tmp_path / "report.json"
    assert (
        main(
            [
                "train",
                "--iterations",
                "3",
                "--algorithm",
                algorithm,
                "--seed",
                "7",
                "--blueprint",
                str(blueprint),
                "--checkpoint",
                str(checkpoint),
                "--report",
                str(report),
            ]
        )
        == 0
    )
    trained = json.loads(capsys.readouterr().out)
    assert trained == json.loads(report.read_text())
    assert trained["training"]["total_iterations"] == 3
    assert (
        main(
            [
                "resume",
                "--from-checkpoint",
                str(checkpoint),
                "--iterations",
                "2",
                "--blueprint",
                str(blueprint),
            ]
        )
        == 0
    )
    resumed = json.loads(capsys.readouterr().out)
    direct = Trainer(KuhnPoker(), algorithm=algorithm, weighting="linear", seed=7)
    direct.train(5)
    assert resumed["metadata"]["iterations"] == 5
    assert Blueprint.load(blueprint).checksum == Blueprint.from_trainer(direct).checksum


def test_exact_evaluation_and_unsupported_artifact_rule_mismatch(tmp_path, capsys):
    path = tmp_path / "kuhn.json"
    assert main(["train", "--iterations", "3", "--blueprint", str(path)]) == 0
    capsys.readouterr()
    assert main(["evaluate", "--blueprint", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["evaluation"]["nash_conv"] >= 0
    assert report["evaluation"]["exploitability"] is not None
    assert report["coverage"]["uniform_fallback_queries"] == 0
    assert main(["evaluate", "--players", "3", "--blueprint", str(path)]) == 2
    assert "incompatible" in json.loads(capsys.readouterr().err)["error"]


def test_budget_stop_is_reported_without_claiming_training(tmp_path, capsys):
    path = tmp_path / "untrained.json"
    assert main(["train", "--max-nodes", "0", "--blueprint", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["training"]["stop_reason"] == "node_budget"
    assert report["training"]["completed_iterations"] == 0
    assert report["coverage"]["saved_information_sets"] == 0
    assert any("entirely untrained" in warning for warning in report["warnings"])
    assert main(["evaluate", "--require-coverage", "--blueprint", str(path)]) == 2
    assert "does not cover" in json.loads(capsys.readouterr().err)["error"]


@pytest.mark.parametrize("abstraction", ["none", "suit_canonical"])
def test_holdem_sparse_training_and_sampled_evaluation_only(tmp_path, capsys, abstraction):
    path = tmp_path / "holdem.json"
    config = [
        "--game",
        "holdem",
        "--stacks",
        "4,4",
        "--bet-fractions",
        "none",
        "--abstraction",
        abstraction,
    ]
    assert main(["train", *config, "--blueprint", str(path)]) == 2
    assert "mccfr" in json.loads(capsys.readouterr().err)["error"]
    assert (
        main(
            [
                "train",
                *config,
                "--algorithm",
                "mccfr",
                "--iterations",
                "1",
                "--blueprint",
                str(path),
            ]
        )
        == 0
    )
    trained = json.loads(capsys.readouterr().out)
    assert trained["training"]["completed_iterations"] == 1
    assert trained["metadata"]["rules_version"].endswith("|suit-canonical-v1") == (
        abstraction == "suit_canonical"
    )
    assert main(["evaluate", *config, "--blueprint", str(path)]) == 2
    assert "exact evaluation" in json.loads(capsys.readouterr().err)["error"]
    assert (
        main(
            [
                "evaluate",
                *config,
                "--mode",
                "sampled",
                "--samples",
                "2",
                "--blueprint",
                str(path),
            ]
        )
        == 0
    )
    sampled = json.loads(capsys.readouterr().out)
    assert sampled["evaluation"]["samples"] == 2
    assert "nash_conv" not in sampled["evaluation"]
    assert sampled["coverage"]["uniform_fallback_queries"] > 0


def test_invalid_configuration_and_missing_files_are_clean_errors(tmp_path, capsys):
    path = tmp_path / "missing.json"
    assert main(["evaluate", "--blueprint", str(path)]) == 2
    assert "error" in json.loads(capsys.readouterr().err)
    assert main(["train", "--game", "leduc", "--players", "3", "--blueprint", str(path)]) == 2
    assert "two players" in json.loads(capsys.readouterr().err)["error"]
    assert main(["train", "--game", "holdem", "--stacks", "4,4,4", "--blueprint", str(path)]) == 2
    assert "one integer" in json.loads(capsys.readouterr().err)["error"]


def test_output_paths_cannot_overwrite_each_other_or_input_artifacts(tmp_path, capsys):
    path = tmp_path / "blueprint.json"
    assert main(["train", "--blueprint", str(path), "--checkpoint", str(path)]) == 2
    assert "distinct" in json.loads(capsys.readouterr().err)["error"]
    assert not path.exists()
    assert main(["evaluate", "--blueprint", str(path), "--report", str(path)]) == 2
    assert "overwrite" in json.loads(capsys.readouterr().err)["error"]


def test_synthetic_adaptation_defaults_to_baseline_without_samples(tmp_path, capsys):
    path = tmp_path / "kuhn.json"
    assert main(["train", "--iterations", "100", "--blueprint", str(path)]) == 0
    capsys.readouterr()
    assert main(["adapt", "--blueprint", str(path), "--opponent-mode", "call"]) == 0
    fallback = json.loads(capsys.readouterr().out)
    assert not fallback["adaptation"]["accepted"]
    assert fallback["adaptation"]["selected_weight"] == 0
    assert fallback["opponent_source"] == "synthetic_continuation_policy"
    assert (
        main(
            [
                "adapt",
                "--blueprint",
                str(path),
                "--opponent-mode",
                "call",
                "--synthetic-samples",
                "100",
                "--worst-case-loss",
                "0.05",
            ]
        )
        == 0
    )
    accepted = json.loads(capsys.readouterr().out)["adaptation"]
    assert accepted["accepted"]
    assert accepted["model_gain"] > 0
    assert accepted["candidate_worst_case"] >= accepted["baseline_worst_case"] - 0.05 - 1e-9


def test_multiplayer_adaptation_preserves_baseline(tmp_path, capsys):
    path = tmp_path / "kuhn3.json"
    assert main(["train", "--players", "3", "--iterations", "2", "--blueprint", str(path)]) == 0
    capsys.readouterr()
    assert (
        main(["adapt", "--players", "3", "--blueprint", str(path), "--synthetic-samples", "100"])
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert not report["adaptation"]["accepted"]
    assert report["adaptation"]["reason"] == "multiplayer_guard_unsupported"
