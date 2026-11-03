"""Independent expected-count oracle and reproducible sampled averaging."""

import json
from fractions import Fraction as F
from itertools import product
from random import Random

import pytest

from texas_game_core.leduc import LeducPoker
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer, _TableEntry
from texas_strategy.pluribus import ALGORITHM_VERSION, SAMPLED_ALGORITHM_VERSION, PluribusConfig


class AverageTree:
    num_players = 3
    final_betting_round = 0
    game_id = "average-tree"
    rules_version = "average-tree-v1"

    def initial_state(self):
        return ()

    def actor(self, state):
        if state and (state[0] == "stop" or len(state) == 6):
            return None
        return (0, -1, 1, 2, 0, 1)[len(state)]

    def betting_round(self, state):
        return 0

    def active_players(self, state):
        return (0, 1, 2)

    def legal_actions(self, state):
        if not state:
            return ("continue", "stop")
        if len(state) == 2:
            return ("L", "R")
        if len(state) == 3:
            return ("X", "Y") if state[2] == "L" else ("X", "Y", "Z")
        return ("U", "V") if len(state) == 4 else ("S", "T")

    def information_key(self, state, player):
        if not state:
            return "root"
        if player == 0:
            return f"{state[1]}:{state[3]}"  # The first opponent's L/R action is hidden.
        return str(state)

    def chance_outcomes(self, state):
        return (("red", 1 / 3), ("blue", 2 / 3))

    def transition(self, state, action):
        return (*state, action)

    def terminal_payoffs(self, state):
        return (0, 0, 0)  # Freeze all seeded regrets during this expectation experiment.


def seeded_tree(samples=1):
    trainer = Trainer(
        AverageTree(),
        algorithm="pluribus",
        pluribus={
            "average_mode": "sampled_uniform",
            "average_samples": samples,
            "discount_until": 0,
            "snapshot_interval": 1000,
        },
    )
    pending = [()]
    while pending:
        state = pending.pop()
        actor = trainer.game.actor(state)
        if actor is None:
            continue
        if actor == -1:
            actions = tuple(a for a, _ in trainer.game.chance_outcomes(state))
        else:
            actions = trainer.game.legal_actions(state)
            key = actor, trainer.game.information_key(state, actor)
            regrets = (
                [1, 3]
                if not state
                else ([2, 1] if state[3] == "X" else [1, 4])
                if actor == 0
                else [1, *([0] * (len(actions) - 1))]
            )
            trainer._table[key] = _TableEntry(actions, regrets, [0.0] * len(actions))
            trainer._betting_rounds[key] = 0
        pending.extend(trainer.game.transition(state, action) for action in actions)
    return trainer


class ChoiceTape:
    def __init__(self, tape):
        self.tape = iter(tape)

    def random(self):
        kind, value = next(self.tape)
        assert kind == "random"
        return float(value)

    def randrange(self, size):
        kind, value = next(self.tape)
        assert kind == "choice" and 0 <= value < size
        return value


def all_average_paths():
    # Enumerate the sampler's entire outcome space, including irrelevant future actions.
    yield F(3, 4), [("random", F(5, 8))]
    for (card, card_p), hidden in product(((0, F(1, 3)), (1, F(2, 3))), range(2)):
        count = 2 + hidden
        for public in range(count):
            policy_u = F(2, 3) if public == 0 else F(1, 5)
            for selected, suffix in product(range(2), range(2)):
                selected_p = policy_u if selected == 0 else 1 - policy_u
                yield (
                    F(1, 4) * card_p / 2 / count * selected_p / 2,
                    [
                        ("random", F(1, 8)),
                        ("random", F(1, 6) if card == 0 else F(2, 3)),
                        ("choice", hidden),
                        ("choice", public),
                        ("random", policy_u / 2 if selected == 0 else (1 + policy_u) / 2),
                        ("choice", suffix),
                    ],
                )


@pytest.mark.parametrize("samples", [1, 4])
def test_exact_expected_counts_include_own_reach_chance_and_all_hidden_histories(
    monkeypatch, samples
):
    expected = {"root": (F(1, 4), F(3, 4))}
    for (card, card_p), (public, multiplicity) in product(
        (("red", F(1, 3)), ("blue", F(2, 3))), (("X", 2), ("Y", 2), ("Z", 1))
    ):
        policy_u = F(2, 3) if public == "X" else F(1, 5)
        mass = card_p * F(1, 4) * multiplicity
        expected[f"{card}:{public}"] = (mass * policy_u, mass * (1 - policy_u))
    totals = {key: [0.0, 0.0] for key in expected}
    total_probability = F(0)
    second_moment = 0.0
    for probability, tape in all_average_paths():
        # Supply every possible player-0 path with its exact probability; other players
        # cannot modify player 0's zero-payoff regrets or average counters.
        def factory(seed, tape=tape):
            player = int(seed.rsplit(":", 2)[1])
            return ChoiceTape(tape) if player == 0 else Random(seed)

        monkeypatch.setattr("texas_strategy.pluribus.Random", factory)
        trainer = seeded_tree(samples)
        assert trainer.train(1).completed_iterations == 1
        for key in expected:
            for action, value in enumerate(trainer._table[(0, key)].strategy_sum):
                totals[key][action] += float(probability) * value
        second_moment += float(probability) * trainer._table[(0, "red:X")].strategy_sum[0] ** 2
        total_probability += probability
    assert total_probability == 1
    for key, values in expected.items():
        assert totals[key] == pytest.approx(tuple(map(float, values)), abs=1e-12)
    assert expected["red:X"][0] == F(1, 9)
    assert second_moment == pytest.approx(float(F(10, 27)))
    assert second_moment < float(F(5, 9))  # Exact one-hot estimator second moment.


def test_sampled_rng_is_separate_and_sample_count_does_not_change_regret_updates():
    common = {"discount_interval": 2, "discount_until": 10, "snapshot_interval": 3}
    trainers = [
        Trainer(LeducPoker(), algorithm="pluribus", seed=31, pluribus=settings)
        for settings in (
            {**common, "average_after": 1000},
            {**common, "average_mode": "sampled_uniform", "average_samples": 1},
            {**common, "average_mode": "sampled_uniform", "average_samples": 16},
        )
    ]
    for trainer in trainers:
        assert trainer.train(6).completed_iterations == 6
    baseline = trainers[0]
    for trainer in trainers[1:]:
        assert trainer._rng.getstate() == baseline._rng.getstate()
        for key in baseline._table.keys() | trainer._table.keys():
            left, right = baseline._table.get(key), trainer._table.get(key)
            actions = (left or right).actions
            assert (left.regrets if left else [0.0] * len(actions)) == (
                right.regrets if right else [0.0] * len(actions)
            )


def test_sampled_average_cancel_checkpoint_and_retry_are_deterministic(tmp_path):
    trainer = Trainer(
        LeducPoker(),
        algorithm="pluribus",
        seed=-29,
        pluribus={
            "average_mode": "sampled_uniform",
            "average_samples": 16,
            "snapshot_interval": 2,
            "discount_interval": 2,
        },
    )
    trainer.train(3)
    checkpoint = tmp_path / "before.json"
    trainer.save_checkpoint(checkpoint)
    expected = Trainer.load_checkpoint(trainer.game, checkpoint)
    assert trainer.train(1, max_nodes=5).completed_iterations == 0
    cancelled = tmp_path / "cancelled.json"
    trainer.save_checkpoint(cancelled)
    assert checkpoint.read_bytes() == cancelled.read_bytes()
    assert trainer.train(3).completed_iterations == expected.train(3).completed_iterations == 3
    assert trainer._table == expected._table
    assert trainer._betting_rounds == expected._betting_rounds
    assert trainer._rng.getstate() == expected._rng.getstate()
    assert (
        Blueprint.from_trainer(trainer).metadata["algorithm_version"] == SAMPLED_ALGORITHM_VERSION
    )


def test_legacy_metadata_serializes_only_the_original_fields_and_continues_exactly(tmp_path):
    old_settings = {
        "strategy_interval": 1,
        "snapshot_interval": 100,
        "average_after": 0,
        "prune_after": 200,
        "discount_interval": 10,
        "discount_until": 400,
        "prune_threshold": -300_000_000.0,
        "regret_floor": -310_000_000.0,
    }
    trainer = Trainer(LeducPoker(), algorithm="pluribus", seed=7, pluribus=old_settings)
    trainer.train(2)
    checkpoint = tmp_path / "legacy.json"
    trainer.save_checkpoint(checkpoint)
    document = json.loads(checkpoint.read_bytes())
    assert document["metadata"]["pluribus"] == old_settings
    assert document["metadata"]["algorithm_version"] == ALGORITHM_VERSION
    resumed = Trainer.load_checkpoint(trainer.game, checkpoint)
    assert resumed.pluribus.average_mode == "enumerated"
    same = tmp_path / "same.json"
    resumed.save_checkpoint(same)
    assert same.read_bytes() == checkpoint.read_bytes()
    trainer.train(2)
    resumed.train(2)
    assert trainer._table == resumed._table
    assert trainer._rng.getstate() == resumed._rng.getstate()
    document["metadata"]["algorithm_version"] = SAMPLED_ALGORITHM_VERSION
    with pytest.raises(ValueError, match="algorithm version"):
        Blueprint(document["metadata"], trainer.average_policy())


@pytest.mark.parametrize(
    "settings",
    [
        {"average_mode": "unknown"},
        {"average_mode": "sampled_uniform", "average_samples": 0},
        {"average_mode": "sampled_uniform", "average_samples": True},
        {"average_mode": "sampled_uniform", "sampled_average_from": -1},
        {"average_mode": "sampled_uniform", "sampled_average_from": True},
        {"average_samples": 2},
        {"sampled_average_from": 1},
    ],
)
def test_sampled_average_configuration_is_strict(settings):
    with pytest.raises(ValueError):
        PluribusConfig.from_dict(settings)


def test_sampled_activation_iteration_does_not_backfill_previous_average_counts():
    trainer = Trainer(
        LeducPoker(),
        algorithm="pluribus",
        pluribus={"average_mode": "sampled_uniform", "sampled_average_from": 3},
    )
    trainer.train(2)
    assert not any(any(e.strategy_sum) for e in trainer._table.values())
    trainer.train(1)
    assert any(any(e.strategy_sum) for e in trainer._table.values())


class LongAveragePath:
    num_players = 2
    final_betting_round = 0
    game_id = "average-overflow"
    rules_version = "average-overflow-v1"

    def initial_state(self):
        return 0

    def actor(self, state):
        return None if state >= 1025 else 1

    def betting_round(self, state):
        return 0

    def active_players(self, state):
        return (0, 1)

    def legal_actions(self, state):
        return ("L", "R")

    def information_key(self, state, player):
        return str(state)

    def transition(self, state, action):
        return state + 1

    def terminal_payoffs(self, state):
        return (0, 0)


def test_nonfinite_importance_weight_aborts_without_clipping_or_partial_progress():
    trainer = Trainer(
        LongAveragePath(), algorithm="pluribus", pluribus={"average_mode": "sampled_uniform"}
    )
    rng = trainer._rng.getstate()
    with pytest.raises(ValueError, match="importance weight"):
        trainer.train(1)
    assert trainer.iterations == 0 and trainer._table == {} and trainer._betting_rounds == {}
    assert trainer._rng.getstate() == rng
