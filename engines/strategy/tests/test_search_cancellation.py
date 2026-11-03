"""Cancellation and a single wall budget must never publish a late search result."""

import time
from random import Random
from types import SimpleNamespace

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_strategy import evaluation, search
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import CancellationError, EvaluationLimitError, TraversalBudget
from texas_strategy.search import DepthLimitedGame, build_public_root, solve_subgame


class UniformPolicy:
    def probabilities(self, game, state):
        actions = game.legal_actions(state)
        return (1 / len(actions),) * len(actions)


def test_cancelled_search_does_not_build_a_root_or_fall_back_to_a_budget_result():
    game = KuhnPoker()
    with pytest.raises(CancellationError) as error:
        solve_subgame(game, game.replay((2, 0)), UniformPolicy(), should_cancel=lambda: True)
    assert not isinstance(error.value, EvaluationLimitError)


def test_public_root_checks_cancellation_after_a_blueprint_query():
    cancelled = False

    class CancellingPolicy(UniformPolicy):
        def probabilities(self, game, state):
            nonlocal cancelled
            cancelled = True
            return super().probabilities(game, state)

    game = KuhnPoker()
    target = game.replay((2, 0), ("bet",))
    with pytest.raises(CancellationError):
        build_public_root(
            game,
            game.public_key(target),
            CancellingPolicy(),
            should_cancel=lambda: cancelled,
        )


@pytest.mark.parametrize("sampled", [False, True])
def test_exact_and_sampled_continuations_stop_inside_policy_evaluation(sampled):
    cancelled = False

    class CancellingPolicy(UniformPolicy):
        def probabilities(self, game, state):
            nonlocal cancelled
            cancelled = True
            return super().probabilities(game, state)

    game = KuhnPoker()
    state = game.replay((2, 0))
    root = build_public_root(game, game.public_key(state), UniformPolicy())
    subgame = DepthLimitedGame(
        game,
        root,
        CancellingPolicy(),
        depth_limit=1,
        sampled_rollouts=sampled,
        should_cancel=lambda: cancelled,
    )
    tail = subgame.transition(subgame.wrap_state(state), "check")
    for _ in range(2):
        tail = subgame.transition(tail, "continue:blueprint")
    with pytest.raises(CancellationError):
        if sampled:
            subgame.sample_chance_outcome(tail, Random(1))
        else:
            subgame.terminal_payoffs(tail)
    assert subgame.rollout_cache_entries == 0


def test_cutoff_choice_checks_cancellation_after_the_underlying_active_player_query(monkeypatch):
    game = KuhnPoker()
    state = game.replay((2, 0))
    root = build_public_root(game, game.public_key(state), UniformPolicy())
    cancelled = False
    subgame = DepthLimitedGame(
        game, root, UniformPolicy(), depth_limit=1, should_cancel=lambda: cancelled
    )
    cutoff = subgame.transition(subgame.wrap_state(state), "check")
    active = KuhnPoker.active_players

    def late_active(self, state):
        nonlocal cancelled
        result = active(self, state)
        cancelled = True
        return result

    monkeypatch.setattr(KuhnPoker, "active_players", late_active)
    with pytest.raises(CancellationError):
        subgame.transition(cutoff, "continue:blueprint")


def test_sampled_transition_checks_cancellation_after_final_terminal_detection(monkeypatch):
    game = KuhnPoker()
    state = game.replay((2, 0))
    root = build_public_root(game, game.public_key(state), UniformPolicy())
    cancelled = False
    subgame = DepthLimitedGame(
        game,
        root,
        UniformPolicy(),
        depth_limit=1,
        sampled_rollouts=True,
        should_cancel=lambda: cancelled,
    )
    tail = subgame.transition(subgame.wrap_state(state), "check")
    for _ in range(2):
        tail = subgame.transition(tail, "continue:blueprint")
    actor = KuhnPoker.actor

    def late_actor(self, state):
        nonlocal cancelled
        result = actor(self, state)
        if state.history == ("check", "check"):
            cancelled = True
        return result

    monkeypatch.setattr(KuhnPoker, "actor", late_actor)
    with pytest.raises(CancellationError):
        subgame.transition(tail, "check")
    assert subgame.rollout_samples == 0


def test_cancel_after_completed_iteration_discards_partial_iteration_and_returns_no_policy(
    monkeypatch,
):
    trainers = []
    enabled, checks = True, 0

    class RecordedTrainer(Trainer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            trainers.append(self)

    def cancel():
        nonlocal checks
        if enabled and trainers and trainers[0].iterations == 1:
            checks += 1
            return checks >= 8
        return False

    monkeypatch.setattr(search, "Trainer", RecordedTrainer)
    game = KuhnPoker()
    with pytest.raises(CancellationError):
        solve_subgame(
            game,
            game.replay((2, 0)),
            UniformPolicy(),
            iterations=4,
            algorithm="mccfr",
            sampled_rollouts=True,
            seed=11,
            should_cancel=cancel,
        )
    interrupted = trainers[0]
    assert interrupted.iterations == 1 and checks == 8
    enabled = False
    control = Trainer(interrupted.game, algorithm="mccfr", weighting="linear", seed=11)
    control.train(1)
    assert interrupted._table == control._table
    assert interrupted._rng.getstate() == control._rng.getstate()


def test_final_average_conversion_is_cancellable_between_entries(monkeypatch):
    cancelled, checks = False, 0
    original = Trainer.average_policy

    def average(self, *, check, **kwargs):
        def stop():
            nonlocal cancelled, checks
            checks += 1
            cancelled = checks >= 2
            check()

        return original(self, check=stop, **kwargs)

    monkeypatch.setattr(Trainer, "average_policy", average)
    game = KuhnPoker()
    with pytest.raises(CancellationError):
        solve_subgame(
            game,
            game.replay((2, 0)),
            UniformPolicy(),
            iterations=2,
            should_cancel=lambda: cancelled,
        )
    assert checks == 2


@pytest.fixture
def clock(monkeypatch):
    current = [time.monotonic()]
    timer = SimpleNamespace(monotonic=lambda: current[0])
    monkeypatch.setattr(search, "time", timer)
    monkeypatch.setattr(evaluation, "time", timer)
    return current


def test_root_and_training_share_the_total_deadline(monkeypatch, clock):
    build, train = search.build_public_root, Trainer.train

    def slow_root(*args, **kwargs):
        result = build(*args, **kwargs)
        clock[0] += 0.6
        return result

    def slow_train(self, *args, **kwargs):
        result = train(self, *args, **kwargs)
        assert result.completed_iterations > 0
        clock[0] += 0.6
        return result

    monkeypatch.setattr(search, "build_public_root", slow_root)
    monkeypatch.setattr(Trainer, "train", slow_train)
    game = KuhnPoker()
    with pytest.raises(EvaluationLimitError, match="deadline"):
        solve_subgame(game, game.replay((2, 0)), UniformPolicy(), iterations=2, deadline_seconds=1)


@pytest.mark.parametrize("cancel", [False, True])
def test_result_is_checked_after_the_last_game_and_policy_operations(monkeypatch, clock, cancel):
    original = search.SearchResult
    cancelled = False

    def finish(*args, **kwargs):
        nonlocal cancelled
        result = original(*args, **kwargs)
        assert result.diagnostics.completed_iterations > 0
        if cancel:
            cancelled = True
        else:
            clock[0] += 2
        return result

    monkeypatch.setattr(search, "SearchResult", finish)
    game = KuhnPoker()
    with pytest.raises(CancellationError if cancel else EvaluationLimitError):
        solve_subgame(
            game,
            game.replay((2, 0)),
            UniformPolicy(),
            iterations=2,
            deadline_seconds=1,
            should_cancel=lambda: cancelled,
        )


@pytest.mark.parametrize("cancel", [False, True])
def test_final_evaluation_payoff_cannot_return_after_cancellation_or_expiry(
    monkeypatch, clock, cancel
):
    game = KuhnPoker()
    state = game.replay((2, 0), ("check", "check"))
    payoff = KuhnPoker.terminal_payoffs
    cancelled = False

    def late_payoff(self, state):
        nonlocal cancelled
        result = payoff(self, state)
        if cancel:
            cancelled = True
        else:
            clock[0] += 2
        return result

    monkeypatch.setattr(KuhnPoker, "terminal_payoffs", late_payoff)
    budget = TraversalBudget(10, 1, lambda: cancelled)
    with pytest.raises(CancellationError if cancel else EvaluationLimitError):
        evaluation.evaluate_policy(game, UniformPolicy(), start_state=state, budget=budget)
