"""Business observations are independent of simulated game state and network ACKs."""

import copy
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from texas_analysis.app import create_app
from texas_analysis.capture_contract import BusinessBatch, BusinessEvent
from texas_analysis.network_stream import NetworkStore

ORIGIN = "chrome-extension://" + "a" * 32
TOKEN = "synthetic-test-token-not-an-account-credential"
FIXTURE = Path(__file__).resolve().parents[3] / "fixtures/hands/wpk-observed-synthetic.json"


def fixture():
    return json.loads(FIXTURE.read_text())


def batch(events):
    return BusinessBatch.model_validate(
        {
            "schema": "tgto.capture/0.1",
            "type": "capture.append",
            "source_id": events[0]["source_id"],
            "events": events,
        }
    )


def wire(events):
    return batch(events).model_dump(by_alias=True)


def client_for(path):
    return TestClient(create_app((ORIGIN,), token=TOKEN, data_dir=path))


def connect(client, *, state=True):
    return client.websocket_connect("/v1/research/stream", headers={"origin": ORIGIN})


def auth(ws, *, state=True):
    ws.send_json(
        {"type": "auth", "token": TOKEN, "capabilities": ["capture_state_v1"] if state else []}
    )
    assert "capture_events_v1" in ws.receive_json()["capabilities"]


def test_frontend_wasm_fixture_replay_restart_and_late_settlement(tmp_path):
    f = fixture()
    events = f["events"]
    for _ in range(2):
        store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
        for start in range(0, len(events), 7):
            part = events[start : start + 7]
            assert store.append(ORIGIN, batch(part)) == part[-1]["sequence"]
        result = store.state(ORIGIN, "synthetic-wpk")
        s = result["state"]
        assert s["hand_id"] == "table-1:3"  # Delayed settlement belongs to hand 2.
        for key in (
            "hero_cards",
            "board",
            "players",
            "actions",
            "turn",
            "closed",
            "street",
            "history_complete",
        ):
            assert s[key] == f["expected"][key], key
        assert s["settlements"][0]["hand_id"] == "table-1:2"
        assert s["settlements"][0]["pool_change"] == 60
        assert result["analysis"]["actions"] == []
        assert result["analysis"]["status"] == "unsupported"
        assert store.state(ORIGIN + "b", "synthetic-wpk") is None
        # Old replay acknowledges only that prefix, not the later durable state.
        assert store.append(ORIGIN, batch(events[:1])) == "1"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0] == len(events)


def test_ws_business_and_network_same_source_independent_acks(tmp_path):
    events = fixture()["events"][:2]
    with client_for(tmp_path) as client, connect(client) as ws:
        auth(ws)
        ws.send_json(wire(events))
        assert ws.receive_json() == {
            "schema": "tgto.capture/0.1",
            "type": "capture.ack",
            "source_id": "synthetic-wpk",
            "through_sequence": "2",
        }
        state = ws.receive_json()
        assert state["type"] == "capture.state" and state["through_sequence"] == "2"
        ws.send_json(
            {
                "schema": "tgto.network/0.1",
                "type": "network.append",
                "source_id": "synthetic-wpk",
                "events": [
                    {
                        "schema": "tgto.network/0.1",
                        "type": "capture_gap",
                        "source_id": "synthetic-wpk",
                        "sequence": "1",
                        "captured_at": events[0]["captured_at"],
                        "data": {"reason": "synthetic"},
                    }
                ],
            }
        )
        assert ws.receive_json()["type"] == "network.ack"
        ws.send_json(wire(events[:1]))
        assert ws.receive_json()["through_sequence"] == "1"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}  # No newer state for an older ACK.
    assert not (tmp_path / "sessions.sqlite3").exists()


def test_old_business_client_no_unsolicited_state(tmp_path):
    with client_for(tmp_path) as client, connect(client) as ws:
        auth(ws, state=False)
        ws.send_json(wire(fixture()["events"][:1]))
        assert ws.receive_json()["type"] == "capture.ack"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


def test_complete_observation_still_requires_rule_money_and_model_anchors(tmp_path):
    snap = copy.deepcopy(next(e for e in fixture()["events"] if e["kind"] == "snapshot"))
    snap["sequence"] = "1"
    snap["data"].update(
        history_complete=True,
        hero_cards=["As", "Kd"],
        board=[],
        street="preflop",
        players=[
            {"player_id": "hero", "seat": 1, "stack": 800},
            {"player_id": "player-1", "seat": 2, "stack": 800},
        ],
        turn={
            "player_id": "hero",
            "legal_actions": ["FOLD", "CALL", "RAISE"],
            "call_amount": 8,
            "min_raise_reported": 16,
            "max_raise_reported": 800,
        },
    )
    with client_for(tmp_path) as client, connect(client) as ws:
        auth(ws)
        ws.send_json(wire([snap]))
        assert ws.receive_json()["type"] == "capture.ack"
        result = ws.receive_json()
        assert result["source_id"] == snap["source_id"]
        assert result["through_sequence"] == "1"
        analysis = result["analysis"]
        assert analysis["status"] == "unsupported"
        assert analysis["reason"] == "unverified_rules_and_strategy_coverage"
        assert analysis["actions"] == []
        assert set(analysis["missing_fields"]) == {
            "verified_rule_profile",
            "supported_strategy_model",
            "hand_participants",
            "button_seat",
            "aligned_starting_stacks",
            "mushroom_ledger_anchor",
            "model_context_mapping",
        }


def test_observed_profiles_are_durable_atomic_and_negotiated(tmp_path):
    events = fixture()["events"]
    with client_for(tmp_path) as client, connect(client) as ws:
        ws.send_json({"type": "auth", "token": TOKEN, "capabilities": ["capture_profiles_v1"]})
        assert "capture_profiles_v1" in ws.receive_json()["capabilities"]
        ws.send_json(wire(events))
        assert ws.receive_json()["type"] == "capture.ack"
        report = ws.receive_json()
        assert report["type"] == "capture.profiles"
        assert report["quality"] == "observed_behavior_learning"
        assert report["identity_scope"] == "origin_source"
        assert report["through_sequence"] == events[-1]["sequence"]
        assert sum(p["action_observations"] for p in report["players"]) > 0
        # Repeated batches must not teach the same action twice.
        ws.send_json(wire(events))
        assert ws.receive_json()["type"] == "capture.ack"
        assert ws.receive_json() == report
        ws.send_json(wire(events[:1]))
        assert ws.receive_json()["through_sequence"] == "1"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    assert store.profiles(ORIGIN, "synthetic-wpk") == report
    assert store.profiles(ORIGIN + "b", "synthetic-wpk") is None
    conflict = copy.deepcopy(events[-1])
    conflict["sequence"] = str(int(conflict["sequence"]) + 2)
    with pytest.raises(ValueError, match="Sequence gap"):
        store.append(ORIGIN, batch([conflict]))
    assert store.profiles(ORIGIN, "synthetic-wpk") == report


def test_existing_capture_history_is_backfilled_once(tmp_path):
    events = fixture()["events"]
    path = tmp_path / "capture.sqlite3"
    store = NetworkStore(path, stream="capture")
    store.append(ORIGIN, batch(events))
    expected = store.profiles(ORIGIN, "synthetic-wpk")
    # Simulate an old database containing raw receipts but no learning projection.
    with sqlite3.connect(path) as db:
        tables = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'capture_learning_%'"
            )
        ]
        assert tables
        for table in tables:
            db.execute(f'DROP TABLE "{table}"')
    for _ in range(2):
        restored = NetworkStore(path, stream="capture")
        assert restored.profiles(ORIGIN, "synthetic-wpk") == expected


def test_atomic_conflicting_replay_and_sequence_gap(tmp_path):
    events = fixture()["events"]
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    store.append(ORIGIN, batch(events[:2]))
    before = store.state(ORIGIN, "synthetic-wpk")
    changed = copy.deepcopy(events[:3])
    changed[1]["captured_at"] = "2026-11-01T00:00:01Z"
    with pytest.raises(ValueError, match="Conflicting sequence"):
        store.append(ORIGIN, batch(changed))
    with pytest.raises(ValueError, match="Sequence gap"):
        store.append(ORIGIN, batch(events[3:4]))
    assert store.state(ORIGIN, "synthetic-wpk") == before
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0] == 2


def test_semantic_action_conflict_rolls_back_whole_batch(tmp_path):
    original = next(e for e in fixture()["events"] if e["kind"] == "action_confirmed")
    a = copy.deepcopy(original)
    a["sequence"] = "1"
    b = copy.deepcopy(original)
    b["sequence"] = "2"
    b["data"]["amount_delta"] += 1
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    with pytest.raises(ValueError, match="Conflicting action"):
        store.append(ORIGIN, batch([a, b]))
    assert store.state(ORIGIN, "synthetic-wpk") is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM capture_events").fetchone()[0] == 0


def test_gap_clears_turn_and_cannot_be_healed_by_same_hand_snapshot(tmp_path):
    snap = copy.deepcopy(next(e for e in fixture()["events"] if e["kind"] == "snapshot"))
    snap["sequence"] = "1"
    snap["data"]["history_complete"] = True
    gap = {**snap, "sequence": "2", "kind": "capture_gap", "data": {"reason": "lost"}}
    again = {**snap, "sequence": "3"}
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    store.append(ORIGIN, batch([snap, gap, again]))
    state = store.state(ORIGIN, "synthetic-wpk")["state"]
    assert state["history_complete"] is False and state["turn"] is None and state["gap"] is True


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e.update(kind="unknown"),
        lambda e: e.update(sequence="01"),
        lambda e: e.update(sequence=str(1 << 64)),
        lambda e: e.update(table_id="real-user-table"),
        lambda e: e.update(hand_id="table-2:1"),
        lambda e: e.update(adapter_version="wpk/2"),
        lambda e: e.update(captured_at="2026-11-01T00:00:00"),
        lambda e: e["data"].update(token="forbidden"),
        lambda e: e["data"].update(hero_cards=["As", "As"]),
        lambda e: e["data"].update(players=[{"player_id": "hero", "seat": 1, "stack": True}]),
        lambda e: e["data"].update(
            turn={
                "player_id": "player-1",
                "legal_actions": ["CALL"],
                "call_amount": 2,
                "min_raise_reported": None,
                "max_raise_reported": None,
            }
        ),
    ],
)
def test_business_contract_rejects_bad_inputs(mutation):
    e = copy.deepcopy(next(e for e in fixture()["events"] if e["kind"] == "snapshot"))
    mutation(e)
    with pytest.raises((ValueError, ValidationError)):
        BusinessEvent.model_validate(e)


def test_settlement_conservation_and_immutable_final(tmp_path):
    e = copy.deepcopy(next(e for e in fixture()["events"] if e["kind"] == "settlement"))
    invalid = copy.deepcopy(e)
    invalid["data"]["pool_change"] += 1
    with pytest.raises(ValidationError):
        BusinessEvent.model_validate(invalid)
    e["sequence"] = "1"
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    store.append(ORIGIN, batch([e]))
    invalid = copy.deepcopy(e)
    invalid["sequence"] = "2"
    invalid["data"].update(status="unresolved", reason="point_difference")
    with pytest.raises(ValueError, match="immutable settlement"):
        store.append(ORIGIN, batch([invalid]))


def test_ws_invalid_business_has_no_ack(tmp_path):
    e = fixture()["events"][0]
    payload = wire([e])
    payload["events"][0]["data"]["account_token"] = "forbidden"
    with client_for(tmp_path) as client, connect(client) as ws:
        auth(ws)
        ws.send_json(payload)
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_capture_quota_rolls_back(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture", max_bytes=1)
    with pytest.raises(ValueError, match="quota"):
        store.append(ORIGIN, batch(fixture()["events"][:1]))
    assert store.state(ORIGIN, "synthetic-wpk") is None
