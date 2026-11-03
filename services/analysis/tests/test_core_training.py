import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from texas_analysis.app import create_app
from texas_analysis.core_training import CORE_GAME, CoreTraining
from texas_analysis.game_factory import make_game
from texas_analysis.models import CoreTrainingResponse
from texas_strategy.cfr import Trainer
from texas_strategy.training_store import backup_checkpoint, open_trainer

ROOT = Path(__file__).resolve().parents[3]
HEADERS = {"X-Texas-Local": "1"}
ORIGIN = "chrome-extension://" + "b" * 32


def wait_for(core, predicate, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = core.status()
        if predicate(status):
            return status
        assert status.phase not in ("error", "limited"), status
        time.sleep(0.03)
    pytest.fail(f"Worker did not reach the expected state: {core.status()}")


def assert_terminal_not_restarted(core):
    process = core._process
    process.join(timeout=5)
    assert process.exitcode is not None
    for _ in range(4):
        core._recover()
    assert core._process is process
    assert core._recover_at is None and core._recoveries == 0


def saved_trainer(database, snapshot):
    # A paused worker can hold a write transaction: read only committed progress via backup.
    backup_checkpoint(database, snapshot)
    game = make_game(CORE_GAME)
    source = open_trainer(game, snapshot)
    try:
        result = Trainer(
            game,
            algorithm=source.algorithm,
            weighting=source.weighting,
            seed=source.seed,
            exploration=source.exploration,
            pluribus=asdict(source.pluribus),
        )
        result.iterations = source.iterations
        result._table = dict(source._table.items())
        result._betting_rounds = dict(source._betting_rounds.items())
        result._rng.setstate(source._rng.getstate())
        return result
    finally:
        source._store.close()


def assert_same_training(actual, expected):
    assert actual.iterations == expected.iterations
    assert actual._table == expected._table
    assert actual._betting_rounds == expected._betting_rounds
    assert actual._rng.getstate() == expected._rng.getstate()


def test_eight_player_worker_pauses_saves_and_resumes_the_same_core(tmp_path):
    database = tmp_path / "training.sqlite3"
    core = CoreTraining(tmp_path, idle_seconds=0)
    core.start()
    try:
        waiting = wait_for(core, lambda s: s.phase == "waiting_for_idle")
        assert waiting.worker_pid != os.getpid()
        assert waiting.game.num_players == 8 and waiting.game.stack == 800
        assert waiting.game.abstraction == "mushroom_population"
        assert waiting.game.session_hands == 2
        assert database.is_file()
        assert not (tmp_path / "checkpoint.json").exists()
        core.allow_if_idle(research_busy=True)
        assert not core._permission.is_set()
        core.allow_if_idle()
        # Disk-spilled eight-seat iterations take longer on shared CI CPUs.
        wait_for(core, lambda s: s.iterations >= 1, seconds=240)
        with core.foreground():
            paused = wait_for(core, lambda s: s.phase == "paused")
            first = saved_trainer(database, tmp_path / "first.sqlite3")
            assert first.iterations == paused.iterations
            core.allow_if_idle()
            assert not core._permission.is_set()
            time.sleep(0.15)
            unchanged = saved_trainer(database, tmp_path / "paused.sqlite3")
            assert_same_training(unchanged, first)
            assert core.status().worker_pid == paused.worker_pid
            assert not core.status().recommendation_ready
    finally:
        core.close()
    assert not core._process.is_alive()
    # The legacy import bound must not limit an existing database or later iterations.
    resumed = CoreTraining(tmp_path, idle_seconds=0, checkpoint_limit=1)
    resumed.start()
    resumed.allow_if_idle()
    try:
        wait_for(resumed, lambda s: s.iterations > first.iterations, seconds=240)
        with resumed.foreground():
            wait_for(resumed, lambda s: s.phase == "paused")
            actual = saved_trainer(database, tmp_path / "resumed.sqlite3")
    finally:
        resumed.close()
    additional = actual.iterations - first.iterations
    assert first.train(additional).completed_iterations == additional
    assert_same_training(actual, first)
    assert not (tmp_path / "checkpoint.json").exists()
    assert not (tmp_path / "checkpoint.next.json").exists()


def test_legacy_import_is_preserved_and_existing_database_takes_precedence(tmp_path):
    checkpoint, database = tmp_path / "checkpoint.json", tmp_path / "training.sqlite3"
    original = Trainer(make_game(CORE_GAME), algorithm="pluribus", seed=42)
    original.train(1)
    original.save_checkpoint(checkpoint)
    legacy = checkpoint.read_bytes()
    core = CoreTraining(tmp_path, idle_seconds=0)
    core.start()
    try:
        wait_for(core, lambda s: s.phase == "paused" and s.iterations == 1)
        imported = saved_trainer(database, tmp_path / "imported.sqlite3")
        assert_same_training(imported, original)
        core.allow_if_idle()
        wait_for(core, lambda s: s.iterations >= 2, seconds=240)
        with core.foreground():
            wait_for(core, lambda s: s.phase == "paused")
            trained = saved_trainer(database, tmp_path / "trained.sqlite3")
    finally:
        core.close()
    assert checkpoint.read_bytes() == legacy
    resumed = CoreTraining(tmp_path, checkpoint_limit=1)
    resumed.start()
    try:
        wait_for(
            resumed,
            lambda s: s.worker_pid == resumed._process.pid and s.phase == "paused",
        )
        restored = saved_trainer(database, tmp_path / "restored.sqlite3")
        assert_same_training(restored, trained)
        assert checkpoint.read_bytes() == legacy
    finally:
        resumed.close()


def test_legacy_import_size_limit_does_not_create_empty_progress_or_restart(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    original = Trainer(make_game(CORE_GAME), algorithm="pluribus", seed=42)
    original.save_checkpoint(checkpoint)
    legacy = checkpoint.read_bytes()
    core = CoreTraining(tmp_path, checkpoint_limit=len(legacy) - 1)
    core.start()
    try:
        status = wait_for(core, lambda s: s.phase == "error")
        assert "ValueError" in status.reason
        assert not (tmp_path / "training.sqlite3").exists()
        assert checkpoint.read_bytes() == legacy
        assert_terminal_not_restarted(core)
    finally:
        core.close()


def test_a_second_service_cannot_write_the_same_core(tmp_path):
    owner = CoreTraining(tmp_path)
    duplicate = CoreTraining(tmp_path)
    owner.start()
    try:
        wait_for(owner, lambda s: s.phase == "waiting_for_idle")
        duplicate.start()
        status = wait_for(duplicate, lambda s: s.phase == "error")
        assert "Another worker" in status.reason
        assert owner.status().worker_pid == owner._process.pid
        assert_terminal_not_restarted(duplicate)
    finally:
        duplicate.close()
        owner.close()


def test_invalid_import_is_not_reset_or_overwritten(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_bytes(b"invalid imported checkpoint")
    core = CoreTraining(tmp_path, idle_seconds=0)
    core.start()
    core.allow_if_idle()
    try:
        status = wait_for(core, lambda s: s.phase == "error")
        assert "ValueError" in status.reason
        assert checkpoint.read_bytes() == b"invalid imported checkpoint"
        assert_terminal_not_restarted(core)
    finally:
        core.close()


def test_corrupt_database_does_not_fall_back_to_legacy_or_restart(tmp_path):
    checkpoint, database = tmp_path / "checkpoint.json", tmp_path / "training.sqlite3"
    Trainer(make_game(CORE_GAME), algorithm="pluribus", seed=42).save_checkpoint(checkpoint)
    legacy = checkpoint.read_bytes()
    database.write_bytes(b"corrupted database with newer training")
    core = CoreTraining(tmp_path, idle_seconds=0)
    core.start()
    core.allow_if_idle()
    try:
        status = wait_for(core, lambda s: s.phase == "error")
        assert "ValueError" in status.reason
        assert database.read_bytes() == b"corrupted database with newer training"
        assert checkpoint.read_bytes() == legacy
        assert_terminal_not_restarted(core)
    finally:
        core.close()


def test_service_starts_and_stops_the_worker_without_a_training_request(tmp_path):
    app = create_app(data_dir=tmp_path, auto_train=True)
    core = app.state.core_training
    with TestClient(app, base_url="http://127.0.0.1") as client:
        wait_for(core, lambda s: s.phase == "waiting_for_idle")
        response = client.get("/v1/core-training", headers=HEADERS)
        assert response.status_code == 200
        schema = json.loads(
            (ROOT / "packages/protocol/schemas/core-training-response.schema.json").read_text()
        )
        Draft202012Validator(schema).validate(response.json())
        assert response.json()["objective"] == "expected_net_points"
        assert client.get("/v1/core-training").status_code == 403
        for _ in range(3):
            assert client.get("/v1/health").status_code == 200
        assert core._last_activity < time.monotonic()
    assert not core._process.is_alive()


def test_health_status_and_heartbeat_allow_idle_but_requests_and_capture_pause(tmp_path):
    app = create_app(data_dir=tmp_path, auto_train=False)
    core = app.state.core_training
    with TestClient(app, base_url="http://127.0.0.1") as client:
        core._permission.set()
        previous = core._last_activity
        assert client.get("/v1/health").status_code == 200
        assert client.get("/v1/core-training", headers=HEADERS).json()["phase"] == "disabled"
        with client.websocket_connect(
            "ws://127.0.0.1/v1/research/stream", headers={"Origin": ORIGIN}
        ) as ws:
            ws.send_json({"type": "auth", "capabilities": ["capture_state_v1"]})
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
            assert core._last_activity == previous
            events = json.loads((ROOT / "fixtures/hands/wpk-observed-synthetic.json").read_text())[
                "events"
            ]
            ws.send_json(
                {
                    "schema": "tgto.capture/0.1",
                    "type": "capture.append",
                    "source_id": events[0]["source_id"],
                    "events": events,
                }
            )
            assert ws.receive_json()["type"] == "capture.ack"
            assert ws.receive_json()["type"] == "capture.state"
            assert not core._permission.is_set()
            assert core._last_activity > previous
        core._permission.set()
        assert client.post("/v1/analyze", headers=HEADERS, json={}).status_code == 422
        assert not core._permission.is_set()


@pytest.mark.parametrize("idle", [-1, float("nan"), float("inf")])
def test_invalid_idle_interval_is_rejected(tmp_path, idle):
    with pytest.raises(ValueError, match="Idle"):
        CoreTraining(tmp_path, idle_seconds=idle)


def test_supervisor_recovers_killed_worker_from_identical_checkpoint_and_rng(tmp_path):
    app = create_app(data_dir=tmp_path, auto_train=True)
    core = app.state.core_training
    core.idle_seconds = 0
    database = core.directory / "training.sqlite3"
    with TestClient(app, base_url="http://127.0.0.1"):
        wait_for(core, lambda s: s.iterations >= 1, seconds=240)
        with core.foreground():
            paused = wait_for(core, lambda s: s.phase == "paused")
            direct = saved_trainer(database, tmp_path / "before-kill.sqlite3")
            killed = core._process
            killed.kill()
            killed.join(timeout=5)
            assert killed.exitcode != 0
            recovered = wait_for(
                core,
                lambda s: (
                    s.worker_pid not in (None, paused.worker_pid)
                    and s.phase == "paused"
                    and s.iterations == paused.iterations
                ),
            )
            assert "recovery completed (1/3)" in recovered.reason
            restored = saved_trainer(database, tmp_path / "after-kill.sqlite3")
            assert_same_training(restored, direct)
            assert core._process.pid != killed.pid
        wait_for(core, lambda s: s.iterations > direct.iterations, seconds=240)
        with core.foreground():
            wait_for(core, lambda s: s.phase == "paused")
            resumed = saved_trainer(database, tmp_path / "after-recovery.sqlite3")
    assert not core._process.is_alive()
    additional = resumed.iterations - direct.iterations
    assert direct.train(additional).completed_iterations == additional
    assert_same_training(resumed, direct)


class FakeProcess:
    def __init__(self, pid):
        self.pid, self.exitcode = pid, None

    def start(self):
        pass

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self.exitcode is None

    def terminate(self):
        self.exitcode = -15


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    core = CoreTraining(tmp_path)
    clock, processes = [100.0], []

    def process_factory(**_):
        process = FakeProcess(1000 + len(processes))
        processes.append(process)
        return process

    core._context = SimpleNamespace(Process=process_factory)
    monkeypatch.setattr("texas_analysis.core_training.monotonic", lambda: clock[0])
    yield core, clock, processes
    core.close()


def test_recovery_backoff_is_one_two_four_seconds_and_never_resets_its_limit(lifecycle):
    core, clock, processes = lifecycle
    core.start()
    for attempt, delay in enumerate((1, 2, 4), start=1):
        core._process.exitcode = -9
        core._recover()
        status = core.status()
        assert status.phase == "starting" and f"recovery {attempt}/3 in {delay}s" in status.reason
        clock[0] += delay - 0.01
        core._recover()
        assert len(processes) == attempt
        clock[0] += 0.01
        core._recover()
        assert len(processes) == attempt + 1
        assert core._process.is_alive()
        healthy = CoreTrainingResponse(
            enabled=True, game=CORE_GAME, phase="training", worker_pid=core._process.pid
        )
        (core.directory / "progress.json").write_text(healthy.model_dump_json())
        core._recover()
        assert core.status().phase == "training"
    core._process.exitcode = -9
    for _ in range(5):
        clock[0] += 1000
        core._recover()
        core.start()
    assert len(processes) == 4
    assert core.status().phase == "error"
    assert "recovery limit reached (3/3)" in core.status().reason


@pytest.mark.parametrize("phase", ["limited", "error", "stopped"])
def test_terminal_state_is_respected_only_when_it_belongs_to_the_exited_worker(lifecycle, phase):
    core, clock, processes = lifecycle
    core.start()
    state = CoreTrainingResponse(
        enabled=True, game=CORE_GAME, phase=phase, worker_pid=core._process.pid
    )
    (core.directory / "progress.json").write_text(state.model_dump_json())
    core._process.exitcode = (
        -9
    )  # Even a signal after a deterministic terminal result is not retried.
    core._recover()
    clock[0] += 100
    core._recover()
    assert len(processes) == 1 and core.status().phase == phase
    state.worker_pid -= 1  # A stale status from an older process must not suppress recovery.
    (core.directory / "progress.json").write_text(state.model_dump_json())
    core._recover()
    clock[0] += 1
    core._recover()
    assert len(processes) == 2


def test_spawn_failure_consumes_recovery_budget_instead_of_breaking_supervision(lifecycle):
    core, clock, _ = lifecycle
    core.start()
    core._process.exitcode = -9
    attempts = []

    def fail_start(**_):
        attempts.append(True)
        raise OSError("process resources unavailable")

    core._context = SimpleNamespace(Process=fail_start)
    for _ in range(8):
        core._recover()
        clock[0] += 10
    assert len(attempts) == 3
    assert core.status().phase == "error" and "limit reached" in core.status().reason


def test_recovery_replaces_child_signals_without_reopening_the_parent_lifecycle(lifecycle):
    core, clock, _ = lifecycle
    core.start()
    core._permission.set()
    old_permission, old_stop, parent_stop = core._permission, core._worker_stopped, core._stopped
    core._process.exitcode = -9
    core._recover()
    clock[0] += 1
    core._recover()
    assert core._stopped is parent_stop and not parent_stop.is_set()
    assert core._permission is not old_permission and core._worker_stopped is not old_stop
    assert core._permission.is_set() and not core._worker_stopped.is_set()
    # A stale child's signal writes cannot pause or stop its replacement.
    old_permission.clear()
    old_stop.set()
    assert core._permission.is_set() and not core._worker_stopped.is_set()
    core.close()
    assert parent_stop.is_set() and core._worker_stopped.is_set()


@pytest.mark.parametrize("started", [False, True])
def test_close_disables_start_and_any_scheduled_recovery(lifecycle, started):
    core, clock, processes = lifecycle
    if started:
        core.start()
        core._process.exitcode = -9
        core._recover()
        assert core._recover_at is not None
    count = len(processes)
    core.close()
    clock[0] += 100
    core._recover()
    core.start()
    assert len(processes) == count and core._recover_at is None
    assert core.status().phase == "stopped"


@pytest.mark.parametrize("recovering", [False, True])
def test_close_waits_for_in_progress_start_and_cannot_leave_an_orphan(lifecycle, recovering):
    core, clock, processes = lifecycle
    if recovering:
        core.start()
        core._process.exitcode = -9
        core._recover()
        clock[0] += 1
    entered, release, closing = Event(), Event(), Event()
    factory = core._context.Process
    failures = []

    def blocked_factory(**kwargs):
        process = factory(**kwargs)

        def start():
            entered.set()
            assert release.wait(5)

        process.start = start
        return process

    def launch():
        try:
            (core._recover if recovering else core.start)()
        except Exception as error:
            failures.append(error)

    def close():
        closing.set()
        core.close()

    core._context = SimpleNamespace(Process=blocked_factory)
    starter, closer = Thread(target=launch), Thread(target=close)
    starter.start()
    try:
        assert entered.wait(5)
        closer.start()
        assert closing.wait(5)
    finally:
        release.set()
        starter.join(timeout=5)
        if closer.ident is not None:
            closer.join(timeout=5)
    assert not failures
    assert not starter.is_alive() and not closer.is_alive()
    assert core._stopped.is_set() and not core._process.is_alive()
    assert len(processes) == (2 if recovering else 1)
    core._recover()
    core.start()
    assert not core._process.is_alive()
