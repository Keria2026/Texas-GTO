"""Initial-only importance roots, exercised through the actual MCCFR trainer."""

from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
from random import Random

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy import blueprint, cfr
from texas_strategy.cfr import Trainer, _TableEntry


class ThreePlayerGame:
    """Two private deals, nonuniform future chance, and a repeated own decision."""

    num_players = 3
    game_id = "three-player-root-test"
    rules_version = "1"
    cards = ((0, 1, 2), (2, 0, 1))
    probe = None

    def __init__(self, zero_second=False):
        self.zero_second = zero_second

    def initial_state(self):
        return ()

    def actor(self, state):
        if not state:
            return -1
        _, board, history = state
        if len(history) == 4:
            return None
        if len(history) == 1 and board is None:
            return -1
        return (0, 1, 2, 0)[len(history)]

    def legal_actions(self, state):
        if self.probe is not None:
            # exploration=1 makes the trainer's opponent proposal exactly 1/2.
            self.probe.probability = F(1, 2)
        return ("stay", "press")

    def information_key(self, state, player):
        world, board, history = state
        return f"{self.cards[world][player]}:{board}:{history}"

    def chance_outcomes(self, state):
        if not state:
            return (
                (("world:0", 1.0), ("world:1", 0.0))
                if self.zero_second
                else (("world:0", 5 / 8), ("world:1", 3 / 8))
            )
        probability = 1 / 4 if state[0] == 0 else 3 / 4
        return (("board:0", probability), ("board:1", 1 - probability))

    def sample_chance_outcome(self, state, rng):
        assert state, "The initial proposal must not also draw an ordinary chance root"
        outcomes = self.chance_outcomes(state)
        rng.probability = F(outcomes[0][1])
        return outcomes[0 if rng.random() < outcomes[0][1] else 1]

    def transition(self, state, action):
        if not state:
            return int(action[-1]), None, ()
        world, board, history = state
        if self.actor(state) == -1:
            return world, int(action[-1]), history
        return world, board, (*history, int(action == "press"))

    def terminal_payoffs(self, state):
        world, board, actions = state
        scores = tuple(
            self.cards[world][p] * (1 + actions[p])
            - 2 * actions[p] * actions[(p + 1) % 3]
            + actions[3] * (p - 1) * (board + 1)
            for p in range(3)
        )
        return tuple(scores[p] - scores[(p + 1) % 3] for p in range(3))


class ProposedGame(ThreePlayerGame):
    def __init__(self, zero_second=False):
        super().__init__(zero_second)
        self.draws = 0

    def sample_training_root(self, rng):
        if isinstance(rng, BranchRng):
            rng.group = self.draws % self.num_players
        self.probe = rng
        self.draws += 1
        rng.probability = F(1, 4)
        world = 0 if rng.random() < 1 / 4 else 1
        q = (1 / 4, 3 / 4)[world]
        w = (5 / 16, 0 if self.zero_second else 3 / 16)[world]
        return (world, None, ()), q, w


def seeded(game, algorithm):
    trainer = Trainer(game, algorithm=algorithm, exploration=1, seed=11)

    def visit(state):
        player = game.actor(state)
        if player is None:
            return
        if player == -1:
            for action, probability in game.chance_outcomes(state):
                if probability:
                    visit(game.transition(state, action))
            return
        key = player, game.information_key(state, player)
        high = (game.cards[state[0]][player] + len(state[2]) + (state[1] or 0)) % 2
        trainer._table[key] = _TableEntry(
            ("stay", "press"), [3.0, 1.0] if high else [1.0, 3.0], [0.0, 0.0]
        )
        for action in game.legal_actions(state):
            visit(game.transition(state, action))

    visit(game.initial_state())
    return trainer


class NeedBranch(Exception):
    def __init__(self, probability):
        self.probability = probability


class BranchRng:
    """Enumerate one traverser's random tape; other traversers have fixed draws.

    Regret/average deltas update only the traverser's information sets and the
    iteration freezes every policy. Linearity thus avoids a Cartesian product
    of all three traversers' independent tapes. Branch probabilities are exact
    Fractions; the real Trainer retains its normal floating-point arithmetic.
    """

    def __init__(self, tape, selected):
        self.tape, self.selected = tape, selected
        self.group, self.index, self.probability = -1, 0, F(1, 2)

    def getstate(self):
        return self.group, self.index, self.probability

    def setstate(self, state):
        self.group, self.index, self.probability = state

    def random(self):
        if self.group != self.selected:
            return 0.5
        if self.index == len(self.tape):
            raise NeedBranch(self.probability)
        second = self.tape[self.index]
        self.index += 1
        return float(
            self.probability + (1 - self.probability) / 2 if second else self.probability / 2
        )


def integrated_iteration(player, zero_second):
    pending, probability_sum = [((), F(1))], F(0)
    sums = defaultdict(lambda: [F(0)] * 4)
    leaves = 0
    while pending:
        tape, probability = pending.pop()
        game = ProposedGame(zero_second)
        trainer = seeded(game, "mccfr")
        initial = deepcopy(trainer._table)
        trainer._rng = BranchRng(tape, player)
        try:
            diagnostics = trainer.train(1)
        except NeedBranch as branch:
            pending.append(((*tape, False), probability * branch.probability))
            pending.append(((*tape, True), probability * (1 - branch.probability)))
            continue
        assert diagnostics.completed_iterations == 1 and game.draws == 3
        assert trainer._rng.index == len(tape)
        probability_sum += probability
        leaves += 1
        for key, entry in trainer._table.items():
            if key[0] == player:
                regrets = [a - b for a, b in zip(entry.regrets, initial[key].regrets, strict=True)]
                for index, value in enumerate(regrets + entry.strategy_sum):
                    sums[key][index] += probability * F(value)
    assert probability_sum == 1 and leaves > 1
    return sums


@pytest.mark.parametrize("zero_second", [False, True])
def test_actual_trainer_expected_regrets_scale_by_Z_and_averages_equal_exact(zero_second):
    exact = seeded(ThreePlayerGame(zero_second), "cfr")
    initial = deepcopy(exact._table)
    assert exact.train(1).completed_iterations == 1
    normalizer = F(5, 16) if zero_second else F(1, 2)
    for player in range(3):
        expected = integrated_iteration(player, zero_second)
        for key, entry in exact._table.items():
            if key[0] == player:
                regrets = [a - b for a, b in zip(entry.regrets, initial[key].regrets, strict=True)]
                assert [float(value) for value in expected[key]] == pytest.approx(
                    [float(normalizer) * value for value in regrets] + entry.strategy_sum,
                    rel=1e-12,
                    abs=1e-12,
                )


class RootStub:
    num_players = 3

    def __init__(self, outcome=("leaf", 1.0, 0.0), callback=None):
        self.outcome, self.callback, self.draws = outcome, callback, 0

    def sample_training_root(self, rng):
        self.draws += 1
        rng.random()
        if self.callback:
            self.callback()
        return self.outcome

    def actor(self, state):
        assert state == "leaf"
        return None

    def terminal_payoffs(self, state):
        return 0, 0, 0


def test_zero_mass_draws_cost_nodes_never_redraw_or_create_average_coverage():
    game = RootStub()
    trainer = Trainer(game, algorithm="mccfr", seed=5)
    control = Random(5)
    for _ in range(3):
        control.random()
    result = trainer.train(1, max_nodes=3)
    assert result.completed_iterations == 1 and result.nodes == game.draws == 3
    assert not trainer._table and not trainer.average_policy().entries
    assert trainer._rng.getstate() == control.getstate()


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_root_draw_budget_abort_rewinds_the_whole_iteration(budget):
    game = RootStub()
    trainer = Trainer(game, algorithm="mccfr", seed=5)
    before = trainer._rng.getstate()
    result = trainer.train(1, max_nodes=budget)
    assert result.completed_iterations == 0 and result.stop_reason == "node_budget"
    assert result.nodes == game.draws == budget
    assert trainer._rng.getstate() == before and not trainer._table


@pytest.mark.parametrize(
    "outcome",
    [
        None,
        ("leaf", 1),
        ["leaf", 1, 1],
        ("leaf", True, 1),
        ("leaf", 0, 1),
        ("leaf", -1, 1),
        ("leaf", 1.01, 1),
        ("leaf", float("nan"), 1),
        ("leaf", float("inf"), 1),
        ("leaf", "0.5", 1),
        ("leaf", 1, True),
        ("leaf", 1, -1),
        ("leaf", 1, float("nan")),
        ("leaf", 1, float("inf")),
        ("leaf", 1, 10**400),
        ("leaf", 1e-320, 1e308),
    ],
)
def test_invalid_proposal_rolls_back_rng_without_reset_or_retry(outcome):
    game = RootStub(outcome)
    trainer = Trainer(game, algorithm="mccfr", seed=5)
    before = trainer._rng.getstate()
    with pytest.raises(ValueError, match="[Tt]raining-root"):
        trainer.train(1)
    assert game.draws == 1 and trainer.iterations == 0 and not trainer._table
    assert trainer._rng.getstate() == before


@pytest.mark.parametrize("mode", ["cancel", "deadline", "exception"])
def test_zero_draw_checks_limits_after_sampler_and_rolls_back(mode, monkeypatch):
    stopped = False

    def after_draw():
        nonlocal stopped
        stopped = True
        if mode == "exception":
            raise RuntimeError("root failed")

    monkeypatch.setattr(cfr, "monotonic", lambda: 20.0 if stopped else 10.0)
    game = RootStub(callback=after_draw)
    trainer = Trainer(game, algorithm="mccfr", seed=5)
    before = trainer._rng.getstate()
    if mode == "exception":
        with pytest.raises(RuntimeError, match="root failed"):
            trainer.train(1)
    else:
        result = trainer.train(
            1,
            should_cancel=lambda: mode == "cancel" and stopped,
            deadline_seconds=1 if mode == "deadline" else None,
        )
        assert result.completed_iterations == 0 and result.stop_reason == (
            "cancelled" if mode == "cancel" else "deadline"
        )
    assert game.draws == 1 and trainer.iterations == 0 and not trainer._table
    assert trainer._rng.getstate() == before


def test_partial_positive_traversal_cancellation_restores_prior_table_and_rng():
    game = ProposedGame()
    trainer = seeded(game, "mccfr")
    before, rng = deepcopy(trainer._table), trainer._rng.getstate()
    result = trainer.train(1, should_cancel=lambda: game.draws >= 2)
    assert game.draws == 2 and result.completed_iterations == 0
    assert trainer._table == before and trainer._rng.getstate() == rng
    assert trainer.iterations == 0


@pytest.mark.parametrize("algorithm", ["cfr", "pluribus"])
def test_root_proposal_is_never_accepted_by_core_algorithms(algorithm):
    with pytest.raises(ValueError, match="requires algorithm=mccfr"):
        Trainer(RootStub(), algorithm=algorithm)


def test_noncallable_hook_is_rejected_and_dynamic_replacement_is_checked():
    game = RootStub()
    game.sample_training_root = 1
    with pytest.raises(ValueError, match="must be callable"):
        Trainer(game, algorithm="mccfr")
    trainer = Trainer(KuhnPoker(), algorithm="cfr")
    trainer.game = RootStub()
    with pytest.raises(ValueError, match="requires algorithm=mccfr"):
        trainer.train(1)


def test_local_root_artifacts_cannot_be_saved_or_imported_through_any_public_entry(tmp_path):
    game = ProposedGame()
    trainer = Trainer(game, algorithm="mccfr")
    output = tmp_path / "local.json"
    for save in (trainer.save_checkpoint, lambda p: blueprint.save_checkpoint(trainer, p)):
        with pytest.raises(ValueError, match="cannot be persisted"):
            save(output)
        assert not output.exists()
    with pytest.raises(ValueError, match="cannot be persisted"):
        blueprint.Blueprint.from_trainer(trainer)
    # Swapping the game cannot erase the local provenance of accumulated data.
    trainer.game = ThreePlayerGame()
    with pytest.raises(ValueError, match="cannot be persisted"):
        trainer.save_checkpoint(output)
    normal = Trainer(ThreePlayerGame(), algorithm="mccfr")
    normal.save_checkpoint(output)
    for load in (Trainer.load_checkpoint, blueprint.load_checkpoint):
        with pytest.raises(ValueError, match="cannot be restored"):
            load(game, output)


@pytest.mark.parametrize("algorithm", ["cfr", "mccfr", "pluribus"])
def test_absent_or_explicitly_disabled_root_hook_keeps_default_table_and_rng(algorithm):
    class DisabledRootKuhn(KuhnPoker):
        sample_training_root = None

    old = Trainer(KuhnPoker(3), algorithm=algorithm, seed=19)
    disabled = Trainer(DisabledRootKuhn(3), algorithm=algorithm, seed=19)
    old.train(7)
    disabled.train(7)
    assert disabled._table == old._table
    assert disabled._rng.getstate() == old._rng.getstate()
