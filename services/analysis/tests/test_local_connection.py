"""Token-free local access still excludes other web origins and rebinding hosts."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from texas_analysis.app import MAX_BODY_BYTES, create_app

ORIGIN = "chrome-extension://" + "b" * 32
HEADERS = {"X-Texas-Local": "1", "Origin": "http://127.0.0.1:5174"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("TEXAS_API_TOKEN", raising=False)
    with TestClient(create_app(data_dir=tmp_path), base_url="http://127.0.0.1") as client:
        yield client


def test_local_workbench_reads_and_writes_without_a_secret(client):
    assert client.get("/v1/blueprints", headers=HEADERS).status_code == 200
    response = client.post("/v1/sessions", headers=HEADERS, json={"seed": 7})
    assert response.status_code == 200
    assert response.json()["state_version"] > 0
    assert client.get("/v1/blueprints").status_code == 403
    assert client.get("/v1/blueprints", headers={"X-Texas-Local": "1"}).status_code == 200
    assert client.post("/v1/sessions", headers=HEADERS, json={"seed": True}).status_code == 422
    assert (
        client.post(
            "/v1/sessions", headers=HEADERS, content=b"x" * (MAX_BODY_BYTES + 1)
        ).status_code
        == 413
    )


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "https://evil.example",
        "http://127.0.0.1:5174.evil.example",
        "chrome-extension://" + "z" * 32,
    ],
)
def test_foreign_and_forged_origins_are_rejected(client, origin):
    assert client.get("/v1/blueprints", headers={**HEADERS, "Origin": origin}).status_code == 403


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "192.168.1.10"])
def test_rebinding_and_non_loopback_hosts_are_rejected(client, host):
    assert client.get("/v1/blueprints", headers={**HEADERS, "Host": host}).status_code == 400


def test_installed_extension_is_allowed_without_configuring_its_id(client):
    response = client.options(
        "/v1/blueprints",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Texas-Local",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert client.get("/v1/blueprints", headers={**HEADERS, "Origin": ORIGIN}).status_code == 200


def test_token_free_websocket_capabilities_persistent_ack_and_state(client):
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[3] / "fixtures/hands/wpk-observed-synthetic.json"
        ).read_text()
    )
    with client.websocket_connect(
        "ws://127.0.0.1/v1/research/stream", headers={"Origin": ORIGIN}
    ) as ws:
        ws.send_json({"type": "auth", "capabilities": ["capture_state_v1"]})
        assert "capture_events_v1" in ws.receive_json()["capabilities"]
        events = fixture["events"]
        ws.send_json(
            {
                "schema": "tgto.capture/0.1",
                "type": "capture.append",
                "source_id": events[0]["source_id"],
                "events": events,
            }
        )
        assert ws.receive_json()["through_sequence"] == events[-1]["sequence"]
        state = ws.receive_json()
        assert state["type"] == "capture.state"
        assert state["state"]["hand_id"] == "table-1:3"


@pytest.mark.parametrize(
    "origin",
    ["", "null", "http://127.0.0.1:5174", "https://h5.wpk.com", "chrome-extension://" + "z" * 32],
)
def test_local_stream_requires_a_real_extension_origin(client, origin):
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(
            "ws://127.0.0.1/v1/research/stream", headers={"Origin": origin}
        ):
            pass
    assert error.value.code == 4403
