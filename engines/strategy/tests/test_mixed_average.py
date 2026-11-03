"""The whole-trajectory proposal must retain the original average's expectation."""

from fractions import Fraction as F
from itertools import product
from random import Random

import pytest
from test_sampled_average import AverageTree, ChoiceTape, all_average_paths, seeded_tree

from texas_game_core.leduc import LeducPoker
from texas_strategy.cfr import Trainer
from texas_strategy.pluribus import MIXTURE_ALGORITHM_VERSION, PluribusConfig


class PassiveTree(AverageTree):
    rules_version = "passive-average-tree-v1"
    names = {
        "L": "call",
        "R": "raise",
        "X": "check",
        "Y": "raise",
        "Z": "fold",
        "S": "call",
        "T": "fold",
    }

    def legal_actions(self, state):
        return tuple(self.names.get(a, a) for a in super().legal_actions(state))

    def transition(self, state, action):
        if self.actor(state) != -1:
            mapping = dict(
                zip(self.legal_actions(state), super().legal_actions(state), strict=True)
            )
            action = mapping[action]
        return super().transition(state, action)


class PartialPassiveTree(PassiveTree):
    names = {k: v for k, v in PassiveTree.names.items() if k not in ("X", "Y", "Z")}


def mixture_paths(passive):
    for p, tape in all_average_paths():
        yield p / 2, [("choice", 1), *tape]
        forced = {"none": (), "ends": (2, 5), "all": (2, 3, 5)}[passive]
        forced = tuple(i for i in forced if i < len(tape))
        if not any(tape[i] != ("choice", 0) for i in forced):
            # Each forced decision is binary; unforced chance/own/opponent draws remain.
            yield (
                p * 2 ** len(forced) / 2,
                [("choice", 0), *(item for i, item in enumerate(tape) if i not in forced)],
            )


@pytest.mark.parametrize("passive", ["none", "ends", "all"])
@pytest.mark.parametrize("samples", [1, 4])
def test_complete_sampler_outcome_space_matches_original_expected_counts(
    monkeypatch, passive, samples
):
    expected = {"root": (F(1, 4), F(3, 4))}
    for (card, p), (action, multiplicity) in product(
        (("red", F(1, 3)), ("blue", F(2, 3))), (("X", 2), ("Y", 2), ("Z", 1))
    ):
        sigma = F(2, 3) if action == "X" else F(1, 5)
        mass = p * F(1, 4) * multiplicity
        expected[f"{card}:{action}"] = (mass * sigma, mass * (1 - sigma))
    totals = {key: [0.0, 0.0] for key in expected}
    total = F(0)
    for p, tape in mixture_paths(passive):

        def factory(seed, tape=tape):
            player = int(seed.rsplit(":", 2)[1])
            return ChoiceTape(tape) if player == 0 else Random(seed)

        monkeypatch.setattr("texas_strategy.pluribus.Random", factory)
        game = {"none": AverageTree, "ends": PartialPassiveTree, "all": PassiveTree}[passive]()
        trainer = seeded_tree(samples, game=game, average_mode="sampled_mixture")
        assert trainer.train(1).completed_iterations == 1
        for key in expected:
            for a, value in enumerate(trainer._table[(0, key)].strategy_sum):
                totals[key][a] += float(p) * value
        total += p
    assert total == 1
    for key, value in expected.items():
        assert totals[key] == pytest.approx(tuple(map(float, value)), abs=1e-12)


def test_future_switch_reproduces_the_legacy_sampler_until_recorded_boundary(tmp_path):
    common = {"average_samples": 4, "sampled_average_from": 1, "snapshot_interval": 2}
    legacy = Trainer(
        LeducPoker(),
        algorithm="pluribus",
        seed=31,
        pluribus={**common, "average_mode": "sampled_uniform"},
    )
    mixed = Trainer(
        LeducPoker(),
        algorithm="pluribus",
        seed=31,
        pluribus={**common, "average_mode": "sampled_mixture", "mixture_from": 4},
    )
    legacy.train(3)
    mixed.train(3)
    assert legacy._table == mixed._table
    assert legacy._rng.getstate() == mixed._rng.getstate()
    path = tmp_path / "before-mixture.json"
    mixed.save_checkpoint(path)
    resumed = Trainer.load_checkpoint(mixed.game, path)
    assert resumed.pluribus.algorithm_version == MIXTURE_ALGORITHM_VERSION
    assert resumed.pluribus.mixture_from == 4
    mixed.train(2)
    resumed.train(2)
    legacy.train(2)
    assert mixed._table == resumed._table and mixed._table != legacy._table
    assert mixed._rng.getstate() == resumed._rng.getstate() == legacy._rng.getstate()


@pytest.mark.parametrize(
    "settings",
    [
        {"average_mode": "sampled_mixture", "mixture_from": -1},
        {"average_mode": "sampled_mixture", "mixture_from": True},
        {"average_mode": "sampled_uniform", "mixture_from": 1},
        {"mixture_from": 1},
    ],
)
def test_invalid_switch_boundary_rejected(settings):
    with pytest.raises(ValueError):
        PluribusConfig.from_dict(settings)
