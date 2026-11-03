import json
from collections import defaultdict
from dataclasses import replace
from math import fsum
from random import Random

import pytest

from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy.cfr import Trainer
from texas_strategy.mushroom_abstraction import MushroomContextGame
from texas_strategy.mushroom_population import MushroomPopulationGame
from texas_strategy.training_store import open_trainer


def root(active=(0, 1, 2, 3), *, weight=1, outstanding=None, button=0):
    return {
        "stacks": [64 if p in active else 0 for p in range(8)],
        "outstanding": list(outstanding or (0,) * 8),
        "waiting_for_top_up": [p for p in range(8) if p not in active],
        "button": button,
        "weight": weight,
    }


def test_default_has_normalized_four_to_eight_support_and_legacy_root():
    game = MushroomPopulationGame.default()
    counts = defaultdict(float)
    initial = game.initial_state()
    for action, probability in game.chance_outcomes(initial):
        state = game.transition(initial, action)
        counts[len(game.active_players(state))] += probability
    assert len(game.roots) == 489
    assert fsum(p for _, p in game.chance_outcomes(initial)) == 1
    assert dict(counts) == pytest.approx({4: 0.125, 5: 0.125, 6: 0.125, 7: 0.125, 8: 0.5})
    legacy = MushroomContextGame(MushroomSessionGame((800,) * 8))
    selected = game.transition(initial, "root:0")
    assert game._game(0).public_key(selected.underlying) == legacy.public_key(
        legacy.initial_state()
    )
    assert game.rules_version == legacy.rules_version
    assert len(game._games) <= 8


def test_exact_sampler_matches_independent_integer_partition_and_probabilities():
    game = MushroomPopulationGame([root(weight=2), root((0, 2, 4, 6), weight=3)], horizon=1)
    actual, expected = Random(17), Random(17)
    for _ in range(100):
        chosen = 0 if expected.randrange(5) < 2 else 1
        action, probability = game.sample_chance_outcome(game.initial_state(), actual)
        assert (action, probability) == (f"root:{chosen}", (2, 3)[chosen] / 5)
    assert actual.getstate() == expected.getstate()


def test_one_root_preserves_complete_pluribus_updates_and_rng():
    spec = root(tuple(range(8)))
    old = MushroomContextGame(MushroomSessionGame(tuple(spec["stacks"]), horizon=1))
    game = MushroomPopulationGame([spec], horizon=1)
    a, b = Trainer(old, algorithm="pluribus", seed=42), Trainer(game, algorithm="pluribus", seed=42)
    assert a.train(2).completed_iterations == b.train(2).completed_iterations == 2
    assert a._table == b._table
    assert a._betting_rounds == b._betting_rounds
    assert a._rng.getstate() == b._rng.getstate()


def test_four_active_players_really_train_and_resume_one_persistent_table(tmp_path):
    specs = [root(weight=2), root((0, 2, 4, 6), outstanding=(20,) * 8, weight=1)]
    game = MushroomPopulationGame(specs, horizon=1)
    path = tmp_path / "population.sqlite3"
    trainer = open_trainer(game, path)
    reference = Trainer(game, algorithm="pluribus", seed=42)
    try:
        assert trainer.train(2).completed_iterations == 2
        assert trainer._store.counts()[1] > 0
        assert reference.train(2).completed_iterations == 2
        assert dict(trainer._table.items()) == reference._table
        context = trainer._store._document["training_context"]
    finally:
        trainer._store.close()
    resumed = open_trainer(MushroomPopulationGame.from_context(context), path)
    try:
        assert resumed.train(1).completed_iterations == reference.train(1).completed_iterations == 1
        assert dict(resumed._table.items()) == reference._table
        assert resumed._rng.getstate() == reference._rng.getstate()
    finally:
        resumed._store.close()


def test_waiting_players_escrow_is_not_lost_or_dealt_private_cards():
    game = MushroomPopulationGame([root(outstanding=(20,) * 8)], horizon=1)
    state = game.transition(game.initial_state(), "root:0")
    table = state.underlying.underlying.table
    assert table.pool == 260
    assert table.participants == (0, 1, 2, 3)
    rng = Random(4)
    while game.actor(state) is not None:
        action = game.sample_chance_outcome(state, rng)[0] if game.actor(state) == -1 else "fold"
        state = game.transition(state, action)
    assert len(game.terminal_payoffs(state)) == 8
    assert fsum(game.terminal_payoffs(state)) == 0
    assert game.terminal_payoffs(state)[4:] == (0, 0, 0, 0)


def test_whole_pool_award_includes_waiting_players_real_entitlements():
    game = MushroomPopulationGame([root(outstanding=(20,) * 8)], horizon=1)
    state = game.transition(game.initial_state(), "root:0")
    rng = Random(4)
    while game.actor(state) is not None:
        actor = game.actor(state)
        if actor == -1:
            action = game.sample_chance_outcome(state, rng)[0]
        else:
            action = "raise_min" if actor == 1 else "fold"
        state = game.transition(state, action)
    assert game.terminal_payoffs(state)[4:] == (-20, -20, -20, -20)
    assert fsum(game.terminal_payoffs(state)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"weight": 0},
        {"weight": True},
        {"button": True},
        {"button": 8},
        {"stacks": [64] * 7},
        {"stacks": [64] * 7 + [True]},
        {"outstanding": [-1] * 8},
        {"waiting_for_top_up": [7, 6, 5, 4]},
        {"waiting_for_top_up": [4, 4]},
        {"extra": 1},
    ],
)
def test_invalid_root_fails_before_training(change):
    with pytest.raises(ValueError):
        MushroomPopulationGame([{**root(), **change}], horizon=1)


def test_duplicate_terminal_and_unknown_manifest_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        MushroomPopulationGame([root(), root(weight=2)])
    with pytest.raises(ValueError, match="four-to-eight"):
        MushroomPopulationGame([root((0, 1, 2))])
    game = MushroomPopulationGame([root()], horizon=1)
    document = json.loads(game.checkpoint_context)
    document["version"] = "future"
    with pytest.raises(ValueError, match="Unsupported"):
        MushroomPopulationGame.from_context(
            json.dumps(document, sort_keys=True, separators=(",", ":"))
        )
    with pytest.raises(ValueError, match="noncanonical"):
        MushroomPopulationGame.from_context(
            json.dumps(json.loads(game.checkpoint_context), indent=2)
        )
    with pytest.raises(ValueError, match="different"):
        game.actor(replace(game.initial_state(), population="other"))


def test_cancellation_rolls_back_root_selection_and_partial_updates():
    game = MushroomPopulationGame([root(), root((0, 2, 4, 6))], horizon=1)
    trainer = Trainer(game, algorithm="pluribus", seed=42)
    rng = trainer._rng.getstate()
    result = trainer.train(1, max_nodes=20)
    assert result.completed_iterations == 0
    assert trainer._rng.getstate() == rng
    assert trainer._table == {}
