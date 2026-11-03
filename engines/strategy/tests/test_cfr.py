"""Independent game values, sampling expectations, and transaction boundaries."""

from collections import defaultdict
from math import isfinite

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy.cfr import Trainer, _TableEntry
from texas_strategy.evaluation import evaluate_strategy
from texas_strategy.policy import PolicyEntry, TabularPolicy


@pytest.mark.parametrize(
    "algorithm,iterations", [("cfr", 1500), ("mccfr", 8000), ("pluribus", 8000)]
)
def test_kuhn_converges_against_independent_best_response(algorithm, iterations):
    game = KuhnPoker()
    trainer = Trainer(game, algorithm=algorithm, weighting="linear", seed=42)
    diagnostics = trainer.train(iterations)
    policy = trainer.average_policy(missing="error")
    report = evaluate_strategy(game, policy)
    assert diagnostics.completed_iterations == iterations
    assert report.policy_values[0] == pytest.approx(-1 / 18, abs=0.006)
    assert report.exploitability < 0.035
    assert len(policy.entries) == 12
    assert policy.fallback_count == 0


def test_three_player_training_preserves_information_sets_and_finite_probabilities():
    game = KuhnPoker(3)
    trainer = Trainer(game, algorithm="mccfr", seed=7)
    trainer.train(1000)
    policy = trainer.average_policy()
    for entry in policy.entries.values():
        assert all(isfinite(p) and 0 <= p <= 1 for p in entry.probabilities)
        assert sum(entry.probabilities) == pytest.approx(1)
    a = game.replay((0, 1, 2))
    b = game.replay((0, 2, 3))
    assert policy.probabilities(game, a) == policy.probabilities(game, b)
    assert evaluate_strategy(game, policy).exploitability is None


class SkewedGame:
    """Two hidden chance branches and a repeated own decision expose reach errors."""

    num_players = 2

    def initial_state(self):
        return ()

    def actor(self, state):
        if not state:
            return -1
        if len(state) == 1:
            return 1
        if state[1] == "block" or (len(state) >= 3 and state[2] == "right") or len(state) == 4:
            return None
        return 0

    def chance_outcomes(self, state):
        return (("H", 0.8), ("L", 0.2))

    def legal_actions(self, state):
        return (
            ("pass", "block")
            if len(state) == 1
            else (("left", "right") if len(state) == 2 else ("up", "down"))
        )

    def information_key(self, state, player):
        return "opponent" if player == 1 else ("first" if len(state) == 2 else "second")

    def transition(self, state, action):
        return (*state, action)

    def terminal_payoffs(self, state):
        if state[1] == "block":
            value = 0.0
        elif state[2] == "right":
            value = 0.5
        elif state[3] == "up":
            value = 1.0 if state[0] == "H" else -2.0
        else:
            value = -1.0 if state[0] == "H" else 2.0
        return value, -value


def seeded_trainer(algorithm, seed=0):
    trainer = Trainer(SkewedGame(), algorithm=algorithm, seed=seed, exploration=0.2)
    trainer._table = {
        (1, "opponent"): _TableEntry(("pass", "block"), [1.0, 3.0], [0.0, 0.0]),
        (0, "first"): _TableEntry(("left", "right"), [3.0, 7.0], [0.0, 0.0]),
        (0, "second"): _TableEntry(("up", "down"), [4.0, 6.0], [0.0, 0.0]),
    }
    return trainer


def test_external_sampling_estimates_exact_regrets_and_own_reach_averages():
    exact = seeded_trainer("cfr")
    initial = {key: tuple(entry.regrets) for key, entry in exact._table.items()}
    exact.train(1)
    # Own reach at the second choice is .3, independent of the opponent's .25.
    assert exact._table[(0, "second")].strategy_sum == pytest.approx([0.24, 0.36])
    sums = defaultdict(lambda: [0.0] * 4)
    samples = 30_000
    for seed in range(samples):
        sampled = seeded_trainer("mccfr", seed)
        sampled.train(1)
        for key, entry in sampled._table.items():
            deltas = [r - old for r, old in zip(entry.regrets, initial[key], strict=True)]
            for index, value in enumerate(deltas + entry.strategy_sum):
                sums[key][index] += value / samples
    for key, entry in exact._table.items():
        regrets = [r - old for r, old in zip(entry.regrets, initial[key], strict=True)]
        assert sums[key] == pytest.approx(regrets + entry.strategy_sum, abs=0.035)


@pytest.mark.parametrize("algorithm", ["cfr", "mccfr", "pluribus"])
def test_node_abort_is_atomic_and_rewinds_rng(algorithm):
    baseline = Trainer(KuhnPoker(), algorithm=algorithm, seed=19)
    interrupted = Trainer(KuhnPoker(), algorithm=algorithm, seed=19)
    baseline.train(20)
    interrupted.train(20)
    before = interrupted.average_policy().entries
    rng = interrupted._rng.getstate()
    stopped = interrupted.train(10, max_nodes=2)
    assert stopped.stop_reason == "node_budget"
    assert stopped.nodes == 2
    assert stopped.completed_iterations == 0
    assert interrupted.average_policy().entries == before
    assert interrupted._rng.getstate() == rng
    baseline.train(10)
    interrupted.train(10)
    assert interrupted._table == baseline._table
    assert interrupted._rng.getstate() == baseline._rng.getstate()


def test_budgets_span_iterations_and_cancellation_discards_partial_iteration():
    trainer = Trainer(KuhnPoker())
    nodes_per_iteration = trainer.train(1).nodes
    diagnostics = trainer.train(5, max_nodes=nodes_per_iteration + 3)
    assert diagnostics.completed_iterations == 1
    assert diagnostics.total_iterations == 2
    assert diagnostics.nodes == nodes_per_iteration + 3
    before = trainer.average_policy().entries
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        return checks >= 5

    assert trainer.train(100, should_cancel=cancel).stop_reason == "cancelled"
    assert trainer.iterations == 2
    assert trainer.average_policy().entries == before
    assert trainer.train(100, deadline_seconds=0).stop_reason == "deadline"


def test_game_exception_rolls_back_all_updates_and_rng():
    trainer = Trainer(KuhnPoker(), algorithm="mccfr", seed=8)
    trainer.train(5)
    before = trainer.average_policy().entries
    rng = trainer._rng.getstate()

    class BrokenGame(KuhnPoker):
        def terminal_payoffs(self, state):
            raise RuntimeError("rollout budget exceeded")

    trainer.game = BrokenGame()
    with pytest.raises(RuntimeError, match="rollout budget"):
        trainer.train(10)
    assert trainer.iterations == 5
    assert trainer.average_policy().entries == before
    assert trainer._rng.getstate() == rng


def test_policy_coverage_and_action_mismatch_are_explicit():
    game = KuhnPoker()
    state = game.replay((0, 1))
    policy = TabularPolicy()
    assert not policy.contains(game, state)
    assert policy.probabilities(game, state) == (0.5, 0.5)
    assert policy.fallback_count == 1
    with pytest.raises(KeyError, match="does not cover"):
        TabularPolicy(missing="error").probabilities(game, state)
    key = (0, game.information_key(state, 0))
    mismatch = TabularPolicy({key: PolicyEntry(("bet", "check"), (0.5, 0.5))})
    with pytest.raises(ValueError, match="legal action order"):
        mismatch.probabilities(game, state)


def test_opponent_only_visits_do_not_falsely_claim_trained_coverage():
    trainer = Trainer(KuhnPoker(), algorithm="mccfr")
    trainer._table[(0, "unaveraged")] = _TableEntry(("check", "bet"), [1.0, 0.0], [0.0, 0.0])
    assert not trainer.average_policy().entries


def test_sampled_training_does_not_persist_empty_opponent_only_entries():
    trainer = Trainer(KuhnPoker(3), algorithm="mccfr", seed=13)
    trainer.train(5)
    assert trainer._table
    assert all(any(entry.regrets) or any(entry.strategy_sum) for entry in trainer._table.values())


def test_exact_chance_sampler_avoids_enumeration_without_changing_training():
    class FastGame(SkewedGame):
        def chance_outcomes(self, state):
            raise AssertionError("Fast sampling should not enumerate chance outcomes")

        def sample_chance_outcome(self, state, rng):
            return ("H", 0.8) if rng.random() < 0.8 else ("L", 0.2)

    reference = Trainer(SkewedGame(), algorithm="mccfr", seed=39)
    fast = Trainer(FastGame(), algorithm="mccfr", seed=39)
    reference.train(100)
    fast.train(100)
    assert fast._table == reference._table
    assert fast._rng.getstate() == reference._rng.getstate()


def test_invalid_chance_sampler_probability_aborts_atomically():
    class InvalidGame(SkewedGame):
        def sample_chance_outcome(self, state, rng):
            rng.random()
            return "H", 0.0

    trainer = Trainer(InvalidGame(), algorithm="mccfr")
    before = trainer._rng.getstate()
    with pytest.raises(ValueError, match="chance sampler"):
        trainer.train(1)
    assert trainer.iterations == 0
    assert trainer._rng.getstate() == before
    assert not trainer._table
