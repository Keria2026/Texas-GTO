"""A newer committed state and an expired request invalidate every analysis exit."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from texas_analysis import research
from texas_analysis.models import AnalyzeRequest, CreateSession, SubmitAction
from texas_analysis.storage import ConflictError, SessionStore
from texas_game_core.kuhn import KuhnPoker
from texas_game_core.leduc import LeducPoker
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import CancellationError, EvaluationLimitError


def blueprint(game):
    trainer = Trainer(game)
    trainer.train(1)
    return Blueprint.from_trainer(trainer)


@pytest.fixture
def context(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    session = store.create(CreateSession())
    policy = blueprint(KuhnPoker())
    manager = SimpleNamespace(blueprint=lambda _: policy)
    request = AnalyzeRequest(
        session_id=session.session_id,
        state_version=session.state_version,
        blueprint_id=str(uuid4()),
        deadline_seconds=1,
    )
    return store, session, manager, request


def advance(store, session):
    return store.act(
        session.session_id,
        SubmitAction(action="check", event_id=str(uuid4()), expected_version=session.state_version),
    )


def test_version_reads_committed_position_without_game_replay(context, monkeypatch):
    store, session, _, _ = context
    assert store.version(session.session_id) == session.state_version
    changed = advance(store, session)
    assert store.version(session.session_id) == changed.state_version
    monkeypatch.setattr(store, "_read", lambda *_: pytest.fail("Version must not rebuild the game"))
    assert store.version(session.session_id) == changed.state_version
    store.delete(session.session_id)
    with pytest.raises(KeyError, match="Unknown session"):
        store.version(session.session_id)


@pytest.mark.parametrize("covered", [False, True])
@pytest.mark.parametrize("deleted", [False, True])
def test_blueprint_loading_race_never_returns_old_coverage(context, covered, deleted):
    store, session, manager, request = context
    policy = manager.blueprint(request.blueprint_id) if covered else blueprint(LeducPoker())

    def load(_):
        store.delete(session.session_id) if deleted else advance(store, session)
        return policy

    manager.blueprint = load
    with pytest.raises(ConflictError, match="State changed"):
        research.analyze(store, manager, request)


def test_loading_consumes_request_budget_and_skips_expired_search(context, monkeypatch):
    store, _, manager, request = context
    clock = [0.0]
    policy = manager.blueprint(request.blueprint_id)
    monkeypatch.setattr(research, "monotonic", lambda: clock[0])

    def load(_):
        clock[0] = 1.01
        return policy

    manager.blueprint = load
    monkeypatch.setattr(research, "solve_subgame", lambda *_a, **_k: pytest.fail("Expired search"))
    result = research.analyze(store, manager, request.model_copy(update={"search": True}))
    assert result.status == "unsupported" and result.strategy_source == "none"
    assert result.actions == [] and result.diagnostics is None
    assert any("deadline exceeded" in warning for warning in result.warnings)


@pytest.mark.parametrize("late_result", [False, True])
def test_expired_search_never_returns_late_result_or_blueprint(context, monkeypatch, late_result):
    store, _, manager, request = context
    clock = [0.0]
    policy = manager.blueprint(request.blueprint_id)
    monkeypatch.setattr(research, "monotonic", lambda: clock[0])

    def load(_):
        clock[0] = 0.25
        return policy

    def search(*_args, **kwargs):
        assert kwargs["deadline_seconds"] == pytest.approx(0.75)
        clock[0] = 1.01
        if not late_result:
            raise EvaluationLimitError("deadline exceeded")
        return SimpleNamespace(
            actions=("check", "bet"),
            probabilities=(0.5, 0.5),
            warnings=(),
            to_dict=lambda: {"late": True},
        )

    manager.blueprint = load
    monkeypatch.setattr(research, "solve_subgame", search)
    result = research.analyze(store, manager, request.model_copy(update={"search": True}))
    assert result.status == "unsupported" and result.strategy_source == "none"
    assert result.actions == [] and result.diagnostics is None
    assert any("deadline exceeded" in warning for warning in result.warnings)


def test_node_limit_can_use_current_covered_blueprint_within_deadline(context, monkeypatch):
    store, _, manager, request = context

    def search(*_args, **_kwargs):
        raise EvaluationLimitError("node budget exceeded")

    monkeypatch.setattr(research, "solve_subgame", search)
    result = research.analyze(store, manager, request.model_copy(update={"search": True}))
    assert result.status == "ready" and result.strategy_source == "blueprint"
    assert sum(action.frequency for action in result.actions) == pytest.approx(1)


@pytest.mark.parametrize("deleted", [False, True])
def test_state_change_cancels_search_without_blueprint_fallback(context, monkeypatch, deleted):
    store, session, manager, request = context
    clock = [0.0]
    monkeypatch.setattr(research, "monotonic", lambda: clock[0])

    def search(*_args, **kwargs):
        cancel = kwargs["should_cancel"]
        assert cancel() is False
        store.delete(session.session_id) if deleted else advance(store, session)
        clock[0] += 0.051
        assert cancel() is True
        raise CancellationError("cancelled")

    monkeypatch.setattr(research, "solve_subgame", search)
    with pytest.raises(ConflictError, match="State changed"):
        research.analyze(store, manager, request.model_copy(update={"search": True}))


def test_node_failure_still_checks_for_new_state(context, monkeypatch):
    store, session, manager, request = context

    def search(*_args, **_kwargs):
        advance(store, session)
        raise EvaluationLimitError("node budget exceeded")

    monkeypatch.setattr(research, "solve_subgame", search)
    with pytest.raises(ConflictError, match="State changed"):
        research.analyze(store, manager, request.model_copy(update={"search": True}))


@pytest.mark.parametrize("deleted", [False, True])
def test_success_inside_poll_throttle_still_forces_final_version_check(
    context, monkeypatch, deleted
):
    store, session, manager, request = context
    monkeypatch.setattr(research, "monotonic", lambda: 0.0)

    def search(*_args, **kwargs):
        cancel = kwargs["should_cancel"]
        assert cancel() is False
        store.delete(session.session_id) if deleted else advance(store, session)
        assert cancel() is False  # No time elapsed, so the query remains throttled.
        return SimpleNamespace(
            actions=("check", "bet"), probabilities=(0.5, 0.5), warnings=(), to_dict=lambda: {}
        )

    monkeypatch.setattr(research, "solve_subgame", search)
    with pytest.raises(ConflictError, match="State changed"):
        research.analyze(store, manager, request.model_copy(update={"search": True}))
