import copy
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from texas_analysis.app import create_app
from texas_analysis.network_stream import NetworkBatch, NetworkStore

ORIGIN = "chrome-extension://" + "a" * 32
TOKEN = "synthetic-test-token-not-an-account-credential"
PATH = "/v1/research/stream"


def event(seq="1", **changes):
    return dict(
        schema="tgto.network/0.1",
        type="capture_gap",
        source_id="fixture-source",
        sequence=seq,
        captured_at="2026-11-01T00:00:00Z",
        data={"reason": "synthetic"},
        **changes,
    )


def batch(*events):
    return dict(
        schema="tgto.network/0.1",
        type="network.append",
        source_id="fixture-source",
        events=list(events or [event()]),
    )


def connect(client, *, status=False):
    ws = client.websocket_connect(PATH, headers={"origin": ORIGIN})
    return ws


def authenticate(ws, status=False):
    auth = {"type": "auth", "token": TOKEN}
    if status:
        auth["capabilities"] = ["network_status_v1"]
    ws.send_json(auth)
    assert ws.receive_json() == dict(
        type="ready", schema_version="1.2", capabilities=["network_events_v1", "network_status_v1"]
    )


def client_for(path):
    return TestClient(create_app((ORIGIN,), token=TOKEN, data_dir=path))


def test_restart_replay_ack_scope_and_status(tmp_path):
    for _ in range(2):
        with client_for(tmp_path) as client, connect(client) as ws:
            authenticate(ws, status=True)
            ws.send_json(batch(event(), event("2")))
            ack = ws.receive_json()
            assert ack["through_sequence"] == "2"
            status = ws.receive_json()
            assert status["source_id"] == ack["source_id"]
            assert status["through_sequence"] == "2"
            assert status["status"] == "blocked" and status["actions"] == []
            assert "hero_cards" in status["missing_fields"]
            # A lost old ACK must never advance beyond the actual resent batch.
            ws.send_json(batch(event()))
            assert ws.receive_json()["through_sequence"] == "1"
            assert ws.receive_json()["through_sequence"] == "1"
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
    with sqlite3.connect(tmp_path / "capture.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM network_events").fetchone()[0] == 2
    assert not (tmp_path / "sessions.sqlite3").exists()  # No synthetic hand was created.


def test_old_frontend_gets_no_unsolicited_status(tmp_path):
    with client_for(tmp_path) as client, connect(client) as ws:
        authenticate(ws)
        ws.send_json(batch())
        assert ws.receive_json()["type"] == "network.ack"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "null"},
        {"origin": "https://h5.wpk.com"},
        {"origin": "chrome-extension://" + "b" * 32},
    ],
)
def test_origin_rejected_before_accept(tmp_path, headers):
    with client_for(tmp_path) as client, pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(PATH, headers=headers):
            pass
    assert error.value.code == 4403
    assert not (tmp_path / "capture.sqlite3").exists()


def test_wrong_token_never_initializes_storage(tmp_path):
    with client_for(tmp_path) as client, connect(client) as ws:
        ws.send_json({"type": "auth", "token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 4401
    assert not (tmp_path / "capture.sqlite3").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "raw",
        "gap",
        "source",
        "zero",
        "u64",
        "numeric",
        "date",
        "endpoint",
        "control",
        "boolean",
        "unknown",
    ],
)
def test_invalid_batches_cannot_write_or_ack(tmp_path, mutation):
    value = batch()
    e = value["events"][0]
    if mutation == "raw":
        e["data"]["payload"] = "sensitive"
    elif mutation == "gap":
        e["sequence"] = "2"
    elif mutation == "source":
        e["source_id"] = "other"
    elif mutation == "zero":
        e["sequence"] = "01"
    elif mutation == "u64":
        e["sequence"] = str(2**64)
    elif mutation == "numeric":
        e["sequence"] = 1
    elif mutation == "date":
        e["captured_at"] = "2026-11-01"
    elif mutation == "endpoint":
        e["data"]["endpoint"] = "wss://example.org/socket?token=secret"
    elif mutation == "control":
        e["type"] = "network_message"
        e["data"] = dict(direction="received", sha256="a" * 64, opcode=9, byte_length=126)
    elif mutation == "boolean":
        e["data"]["opcode"] = True
    else:
        value["type"] = "capture.append"
    with client_for(tmp_path) as client, connect(client) as ws:
        authenticate(ws)
        ws.send_json(value)
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    with sqlite3.connect(tmp_path / "capture.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM network_events").fetchone()[0] == 0


@pytest.mark.parametrize("frame", ["binary", "oversized", "array", "invalid-json", "nested"])
def test_frame_boundary(tmp_path, frame):
    with client_for(tmp_path) as client, connect(client) as ws:
        authenticate(ws)
        if frame == "binary":
            ws.send_bytes(b"{}")
        else:
            ws.send_text(
                {
                    "oversized": " " * (256 * 1024 + 1),
                    "array": "[]",
                    "invalid-json": "{",
                    "nested": "[" * 2000 + "0" + "]" * 2000,
                }[frame]
            )
        assert ws.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_conflicts_transaction_quota_and_origin_namespace(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3", max_bytes=800, max_sources=2)
    original = NetworkBatch.model_validate(batch())
    store.append(ORIGIN, original)
    changed = copy.deepcopy(batch())
    changed["events"][0]["data"]["reason"] = "conflicting"
    with pytest.raises(ValueError, match="Conflicting"):
        store.append(ORIGIN, NetworkBatch.model_validate(changed))
    # A later event exceeding quota must roll back earlier events from the same batch.
    large = batch(event("2"), event("3"))
    large["events"][1]["data"]["reason"] = "x" * 512
    with pytest.raises(ValueError, match="quota"):
        store.append(ORIGIN, NetworkBatch.model_validate(large))
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM network_events").fetchone()[0] == 1
        assert db.execute("SELECT sequence FROM network_heads").fetchone()[0] == "1"
    store.append("another-origin", original)
    with pytest.raises(ValueError, match="quota"):
        store.append("third-origin", original)


def test_large_sequence_is_never_rounded(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3")
    last = 2**53
    with sqlite3.connect(store.path) as db:
        db.execute(
            "INSERT INTO network_heads VALUES(?,?,?,?)", (ORIGIN, "fixture-source", str(last), 0)
        )
    for n in [last + 1, last + 2]:
        assert store.append(ORIGIN, NetworkBatch.model_validate(batch(event(str(n))))) == str(n)
    with sqlite3.connect(store.path) as db:
        assert [
            json.loads(r[0])["sequence"]
            for r in db.execute("SELECT body FROM network_events ORDER BY sequence")
        ] == [str(last + 1), str(last + 2)]
