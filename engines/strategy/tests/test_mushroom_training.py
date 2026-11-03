import pytest

from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer


def test_full_eight_seat_800_point_cross_hand_pluribus_checkpoint_is_reproducible(tmp_path):
    game = MushroomSessionGame(horizon=2)
    settings = {"strategy_interval": 10000, "snapshot_interval": 1}
    direct = Trainer(game, algorithm="pluribus", seed=42, pluribus=settings)
    assert direct.train(2, max_nodes=500_000, deadline_seconds=60).completed_iterations == 2
    first = Trainer(game, algorithm="pluribus", seed=42, pluribus=settings)
    assert first.train(1, max_nodes=500_000, deadline_seconds=60).completed_iterations == 1
    checkpoint = tmp_path / "mushroom-checkpoint.json"
    first.save_checkpoint(checkpoint)
    resumed = Trainer.load_checkpoint(game, checkpoint)
    assert resumed.train(1, max_nodes=500_000, deadline_seconds=60).completed_iterations == 1
    blueprint = Blueprint.from_trainer(resumed)
    assert blueprint.checksum == Blueprint.from_trainer(direct).checksum
    assert blueprint.policy.entries
    assert blueprint.metadata["num_players"] == 8
    assert blueprint.metadata["pluribus"]["strategy_interval"] == 10000
    path = tmp_path / "mushroom-blueprint.json"
    blueprint.save(path)
    assert Blueprint.load(path, game=game).checksum == blueprint.checksum
    with pytest.raises(ValueError, match="incompatible"):
        Blueprint.load(path, game=MushroomSessionGame(horizon=1))


def test_mushroom_large_game_profile_does_not_enumerate_the_average_every_iteration():
    trainer = Trainer(MushroomSessionGame(), algorithm="pluribus")
    assert trainer.pluribus.strategy_interval == 1
    assert trainer.pluribus.average_mode == "sampled_uniform"
    assert trainer.pluribus.average_samples == 16


def test_eight_seat_800_point_model_collects_preflop_averages_in_the_first_iteration():
    from texas_strategy.mushroom_abstraction import MushroomContextGame

    game = MushroomContextGame(MushroomSessionGame(horizon=2))
    trainer = Trainer(game, algorithm="pluribus", seed=42)
    diagnostics = trainer.train(1, max_nodes=500_000, deadline_seconds=30)
    assert diagnostics.completed_iterations == 1
    preflop = [
        entry
        for key, entry in trainer._table.items()
        if trainer._betting_rounds[key] == 0 and any(entry.strategy_sum)
    ]
    assert preflop
    assert any(sum(value > 0 for value in entry.strategy_sum) > 1 for entry in preflop)
    assert all(
        not any(entry.strategy_sum)
        for key, entry in trainer._table.items()
        if trainer._betting_rounds[key] > 0
    )
