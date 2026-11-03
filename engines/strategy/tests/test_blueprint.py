import hashlib
import json

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer


def rewrite_checksum(document):
    document.pop("checksum", None)
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return {**document, "checksum": hashlib.sha256(encoded).hexdigest()}


@pytest.mark.parametrize("algorithm", ["cfr", "mccfr", "pluribus"])
def test_checkpoint_resume_is_identical_to_continuous_training(tmp_path, algorithm):
    game = KuhnPoker()
    continuous = Trainer(game, algorithm=algorithm, weighting="linear", seed=311)
    resumed = Trainer(game, algorithm=algorithm, weighting="linear", seed=311)
    continuous.train(150)
    resumed.train(40)
    path = tmp_path / "checkpoint.json"
    resumed.save_checkpoint(path)
    resumed = Trainer.load_checkpoint(game, path)
    resumed.train(110)
    assert resumed.iterations == continuous.iterations
    assert resumed._table == continuous._table
    assert resumed._rng.getstate() == continuous._rng.getstate()
    assert resumed.average_policy().entries == continuous.average_policy().entries


def test_blueprint_round_trip_checksum_and_exhaustive_game_validation(tmp_path):
    game = KuhnPoker()
    trainer = Trainer(game, weighting="linear")
    trainer.train(10)
    blueprint = Blueprint.from_trainer(trainer)
    path = tmp_path / "nested" / "blueprint.json"
    blueprint.save(path)
    loaded = Blueprint.load(path, game=game)
    assert loaded.checksum == blueprint.checksum
    assert loaded.metadata == blueprint.metadata
    assert loaded.policy.entries == blueprint.policy.entries
    assert loaded.validate_game(game) == 55
    with pytest.raises(ValueError, match="budget"):
        loaded.validate_game(game, max_nodes=1)
    with pytest.raises(ValueError, match="incompatible"):
        Blueprint.load(path, game=KuhnPoker(3))
    with pytest.raises(ValueError, match="size limit"):
        Blueprint.load(path, max_bytes=20)


@pytest.mark.parametrize("kind", ["checkpoint", "blueprint"])
def test_corruption_is_detected_before_loading(tmp_path, kind):
    game = KuhnPoker()
    trainer = Trainer(game)
    trainer.train(2)
    path = tmp_path / "artifact.json"
    if kind == "checkpoint":
        trainer.save_checkpoint(path)
        load = lambda: Trainer.load_checkpoint(game, path)  # noqa: E731
    else:
        Blueprint.from_trainer(trainer).save(path)
        load = lambda: Blueprint.load(path, game=game)  # noqa: E731
    document = json.loads(path.read_text())
    document["metadata"]["iterations"] += 1
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="checksum"):
        load()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["entries"][0].update(probabilities=[-0.1, 1.1]),
        lambda d: d["entries"][0].update(probabilities=[0.2, 0.2]),
        lambda d: d["entries"][0].update(probabilities=[True, False]),
        lambda d: d["entries"].append(d["entries"][0]),
        lambda d: d.update(schema_version=True),
        lambda d: d["metadata"].update(num_players=True),
        lambda d: d["metadata"].update(algorithm_version="unknown"),
        lambda d: d["entries"][0].update(actions=["check", "check"]),
    ],
)
def test_rechecksummed_invalid_payloads_are_rejected(tmp_path, mutation):
    trainer = Trainer(KuhnPoker())
    trainer.train(2)
    path = tmp_path / "artifact.json"
    Blueprint.from_trainer(trainer).save(path)
    document = json.loads(path.read_text())
    mutation(document)
    path.write_text(json.dumps(rewrite_checksum(document)))
    with pytest.raises(ValueError):
        Blueprint.load(path)


def test_wrong_legal_actions_are_caught_by_full_validation_and_query(tmp_path):
    game = KuhnPoker()
    trainer = Trainer(game)
    trainer.train(2)
    path = tmp_path / "artifact.json"
    Blueprint.from_trainer(trainer).save(path)
    document = json.loads(path.read_text())
    for entry in document["entries"]:
        entry["actions"] = ["illegal-a", "illegal-b"]
    path.write_text(json.dumps(rewrite_checksum(document)))
    loaded = Blueprint.load(path, game=game)
    with pytest.raises(ValueError, match="actions"):
        loaded.validate_game(game)
    with pytest.raises(ValueError, match="actions"):
        loaded.policy.probabilities(game, game.replay((0, 1)))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["rng_state"][1].__setitem__(0, -1),
        lambda d: d["rng_state"].__setitem__(0, 2),
        lambda d: d["entries"][0].__setitem__(4, [-1, 1]),
        lambda d: d["entries"][0].__setitem__(3, [0]),
        lambda d: d["entries"][0].__setitem__(2, True),
        lambda d: d["entries"][0].__setitem__(2, 999),
        lambda d: d["entries"][0].append(0),
        lambda d: d["action_sets"][0].append("check"),
        lambda d: d["action_sets"].append(d["action_sets"][0]),
        lambda d: d.update(training_context="wrong-root"),
    ],
)
def test_checkpoint_state_is_strictly_validated(tmp_path, mutation):
    game = KuhnPoker()
    trainer = Trainer(game)
    trainer.train(2)
    path = tmp_path / "checkpoint.json"
    trainer.save_checkpoint(path)
    document = json.loads(path.read_text())
    mutation(document)
    path.write_text(json.dumps(rewrite_checksum(document)))
    with pytest.raises(ValueError):
        Trainer.load_checkpoint(game, path)


def test_json_nonfinite_and_duplicate_fields_rejected(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text('{"checksum": "x", "checksum": "y"}')
    with pytest.raises(ValueError, match="Duplicate"):
        Blueprint.load(path)
    path.write_text('{"value": NaN}')
    with pytest.raises(ValueError, match="Nonfinite"):
        Blueprint.load(path)


def test_checkpoint_rejects_changed_chance_sampling_contract(tmp_path):
    game = KuhnPoker()
    trainer = Trainer(game, algorithm="mccfr")
    trainer.train(2)
    path = tmp_path / "checkpoint.json"
    trainer.save_checkpoint(path)
    document = json.loads(path.read_text())
    document["metadata"]["chance_sampling"] = "game_exact"
    path.write_text(json.dumps(rewrite_checksum(document)))
    with pytest.raises(ValueError, match="chance sampling"):
        Trainer.load_checkpoint(game, path)


@pytest.mark.parametrize("algorithm", ["cfr", "mccfr", "pluribus"])
def test_legacy_checkpoint_remains_importable_and_compaction_preserves_every_value(
    tmp_path, algorithm
):
    game = KuhnPoker()
    trainer = Trainer(game, algorithm=algorithm)
    trainer.train(100)
    compact, legacy = tmp_path / "compact.json", tmp_path / "legacy.json"
    trainer.save_checkpoint(compact)
    document = json.loads(compact.read_text())
    assert document["schema_version"] == 2
    actions = document.pop("action_sets")
    document.pop("training_context")
    document["schema_version"] = 1
    document["entries"] = [
        {
            "player": row[0],
            "information_key": row[1],
            "actions": actions[row[2]],
            "regrets": row[3],
            "strategy_sum": row[4],
            **({"betting_round": row[5]} if algorithm == "pluribus" else {}),
        }
        for row in document["entries"]
    ]
    legacy.write_text(json.dumps(rewrite_checksum(document), separators=(",", ":")))
    assert compact.stat().st_size < legacy.stat().st_size
    first = Trainer.load_checkpoint(game, compact)
    second = Trainer.load_checkpoint(game, legacy)
    assert first._table == second._table == trainer._table
    assert first._rng.getstate() == second._rng.getstate() == trainer._rng.getstate()
    first.train(10)
    second.train(10)
    trainer.train(10)
    assert first._table == second._table == trainer._table
    assert first._rng.getstate() == second._rng.getstate() == trainer._rng.getstate()
