import json

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer
from texas_strategy.cli import main


def test_train_resume_matches_uninterrupted_checkpoint_and_writes_report(tmp_path, capsys):
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
                "mccfr",
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
    direct = Trainer(KuhnPoker(), algorithm="mccfr", weighting="linear", seed=7)
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
