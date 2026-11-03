"""A slow recommendation must not hold up capture ACKs or survive a changed hand."""

from copy import deepcopy
from threading import Event
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from test_capture_recommendation import ORIGIN, batch, call, eight_player_events, fixture_model

from texas_analysis.app import create_app
from texas_analysis.network_stream import NetworkStore
from texas_strategy.evaluation import CancellationError
from texas_strategy.persistent_policy import PersistentPolicy


@pytest.mark.parametrize("cancel_before_query", [True, False])
def test_cancellation_never_returns_advice_and_closes_the_policy(
    tmp_path, monkeypatch, cancel_before_query
):
    events = eight_player_events()
    path = tmp_path / "training.sqlite3"
    fixture_model(path, events)
    stopped, snapshots = Event(), []
    original = PersistentPolicy.lookup

    def lookup(self, *args):
        snapshots.append(self)
        result = original(self, *args)
        stopped.set()
        return result

    monkeypatch.setattr(PersistentPolicy, "lookup", lookup)
    if cancel_before_query:
        stopped.set()
    with pytest.raises(CancellationError):
        call(events, path, should_cancel=stopped.is_set)
    assert bool(snapshots) != cancel_before_query
    for snapshot in snapshots:
        with pytest.raises(ValueError, match="closed"):
            original(snapshot, 0, "synthetic")


def authenticate(ws):
    ws.send_json(
        {"type": "auth", "capabilities": ["capture_state_v1", "capture_recommendations_v1"]}
    )
    assert ws.receive_json()["type"] == "ready"


def append(ws, events):
    ws.send_json(batch(events).model_dump(by_alias=True))
    ack, state = ws.receive_json(), ws.receive_json()
    assert ack["type"] == "capture.ack" and state["type"] == "capture.state"
    assert ack["through_sequence"] == state["through_sequence"] == events[-1]["sequence"]


def test_slow_result_does_not_block_pong_or_new_acks_and_only_latest_is_computed(
    tmp_path, monkeypatch
):
    events = eight_player_events()
    model = tmp_path / "core" / "training.sqlite3"
    model.parent.mkdir()
    fixture_model(model, events)
    started, release, calls = Event(), Event(), []
    original = NetworkStore.recommendation

    def slow(self, origin, source, should_cancel=None):
        calls.append(source)
        result = original(self, origin, source, should_cancel)
        if len(calls) == 1:
            started.set()
            assert release.wait(5), "test did not release the delayed result"
            assert should_cancel()
            # Deliberately return a stale result to exercise the stream's final guard.
        return result

    monkeypatch.setattr(NetworkStore, "recommendation", slow)
    try:
        with TestClient(create_app(data_dir=tmp_path, auto_train=False)) as client:
            with client.websocket_connect(
                "ws://127.0.0.1/v1/research/stream", headers={"origin": ORIGIN}
            ) as ws:
                authenticate(ws)
                append(ws, events)
                assert started.wait(1)
                before = monotonic()
                ws.send_json({"type": "ping"})
                assert ws.receive_json() == {"type": "pong"}
                assert monotonic() - before < 1
                for offset in (1, 2):
                    event = deepcopy(events[-1])
                    event.update(
                        sequence=str(len(events) + offset),
                        kind="capture_gap",
                        data={"reason": "synthetic interruption"},
                    )
                    append(ws, [event])
                assert len(calls) == 1
                release.set()
                result = ws.receive_json()
                assert result["type"] == "capture.recommendation"
                assert result["status"] == "unavailable" and result["actions"] == []
                assert result["through_sequence"] == str(len(events) + 2)
                assert len(calls) == 2
    finally:
        release.set()


@pytest.mark.parametrize("invalid_request", [False, True])
def test_disconnect_or_protocol_error_cancels_and_joins_the_worker(
    tmp_path, monkeypatch, invalid_request
):
    started, finished = Event(), Event()

    def slow(self, origin, source, should_cancel=None):
        started.set()
        deadline = monotonic() + 5
        while monotonic() < deadline and not should_cancel():
            finished.wait(0.005)
        assert should_cancel(), "worker was abandoned without cancellation"
        finished.set()

    monkeypatch.setattr(NetworkStore, "recommendation", slow)
    with TestClient(create_app(data_dir=tmp_path, auto_train=False)) as client:
        with client.websocket_connect(
            "ws://127.0.0.1/v1/research/stream", headers={"origin": ORIGIN}
        ) as ws:
            authenticate(ws)
            append(ws, eight_player_events())
            assert started.wait(1)
            if invalid_request:
                ws.send_json({"type": "invalid"})
                assert ws.receive_json()["type"] == "error"
            else:
                ws.close()
            assert finished.wait(1)
