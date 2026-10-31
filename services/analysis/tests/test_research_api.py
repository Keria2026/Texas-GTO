import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from texas_analysis.app import MAX_BODY_BYTES, LocalBoundary, create_app
from texas_analysis.models import CreateSession, GameConfig, SubmitAction
from texas_analysis.storage import ConflictError, SessionStore

TOKEN = "test-local-token-not-a-real-secret"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(token=TOKEN, data_dir=tmp_path)) as client:
        client.headers.update(HEADERS)
        yield client


def contract(name, response):
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / f"packages/protocol/schemas/{name}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(response)


def session(client, game_id="kuhn", num_players=2):
    response = client.post(
        "/v1/sessions", json={"game": {"game_id": game_id, "num_players": num_players}, "seed": 3}
    )
    assert response.status_code == 200, response.text
    contract("session-response", response.json())
    return response.json()


def train(client):
    response = client.post("/v1/training", json={"iterations": 100, "deadline_seconds": 5.0})
    assert response.status_code == 200, response.text
    job = response.json()
    deadline = time.monotonic() + 10
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
        job = client.get(f"/v1/training/{job['job_id']}").json()
    assert job["status"] == "completed", job
    assert job["diagnostics"]["completed_iterations"] == 100
    assert job["evaluation"]["quality"] == "exact_finite_game"
    assert job["evaluation"]["exploitability"] < 0.1
    contract("training-job", job)
    return job


def test_private_auth_origin_and_streaming_size_boundary(client):
    assert client.post("/v1/sessions", json={}, headers={"Authorization": ""}).status_code == 401
    assert (
        client.get("/v1/blueprints", headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert client.post("/v1/sessions", content=b"x" * (MAX_BODY_BYTES + 1)).status_code == 413
    response = client.options(
        "/v1/sessions",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert response.status_code == 200
    assert client.post("/v1/sessions", json={"unknown": 1}).status_code == 422
    assert client.post("/v1/sessions", json={"seed": True}).status_code == 422
    assert client.get("/v1/sessions/../secrets").status_code in (404, 422)


def test_chunked_size_is_measured_without_content_length():
    called = False
    sent = []
    chunks = iter(
        [
            {"type": "http.request", "body": b"x" * MAX_BODY_BYTES, "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ]
    )

    async def inner(*_):
        nonlocal called
        called = True

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    asyncio.run(
        LocalBoundary(inner, origins=(), token=TOKEN)(
            {
                "type": "http",
                "path": "/v1/sessions",
                "method": "POST",
                "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
            },
            receive,
            send,
        )
    )
    assert not called
    assert sent[0]["status"] == 413


def test_event_idempotency_conflict_replay_restart_and_delete(client, tmp_path):
    first = session(client)
    path = f"/v1/sessions/{first['session_id']}"
    payload = {"action": "check", "event_id": "first", "expected_version": first["state_version"]}
    result = client.post(path + "/actions", json=payload)
    assert result.status_code == 200
    assert result.json()["state_version"] == first["state_version"] + 1
    assert client.post(path + "/actions", json=payload).json() == result.json()
    assert client.post(path + "/actions", json={**payload, "action": "bet"}).status_code == 409
    assert client.post(path + "/actions", json={**payload, "event_id": "stale"}).status_code == 409
    assert (
        client.post(
            path + "/actions",
            json={
                **payload,
                "action": "deal:1,2",
                "event_id": "x",
                "expected_version": result.json()["state_version"],
            },
        ).status_code
        == 422
    )
    store = SessionStore(tmp_path / "sessions.sqlite3")
    assert store.snapshot(first["session_id"]).model_dump() == result.json()
    assert client.delete(path).json() == {"deleted": True}
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("game_id,players", [("kuhn", 3), ("leduc", 2), ("holdem", 6)])
def test_simulated_games_finish_without_exposing_opponent_holes(client, game_id, players):
    current = session(client, game_id, players)
    for index in range(80):
        if current["terminal"]:
            assert sum(current["payoffs"]) == 0
            break
        if game_id == "holdem":
            view = current["observation"]
            for player in view["players"]:
                if player["seat"] != view["hero"]:
                    assert player["cards"] is None
        actions = current["legal_actions"]
        action = next((a for a in ("check", "call", "fold") if a in actions), actions[0])
        response = client.post(
            f"/v1/sessions/{current['session_id']}/actions",
            json={
                "action": action,
                "event_id": str(index),
                "expected_version": current["state_version"],
            },
        )
        assert response.status_code == 200, response.text
        current = response.json()
    else:
        pytest.fail("Synthetic game did not terminate")


def test_training_blueprint_search_and_stale_analysis(client):
    current = session(client)
    job = train(client)
    assert client.get("/v1/blueprints").json()["blueprints"][0]["information_sets"] == 12
    payload = {
        "session_id": current["session_id"],
        "state_version": current["state_version"],
        "blueprint_id": job["blueprint_id"],
    }
    baseline = client.post("/v1/analyze", json=payload)
    assert baseline.status_code == 200, baseline.text
    contract("analysis-response", baseline.json())
    assert baseline.json()["strategy_source"] == "blueprint"
    assert sum(a["frequency"] for a in baseline.json()["actions"]) == pytest.approx(1)
    searched = client.post("/v1/analyze", json={**payload, "search": True, "iterations": 10})
    assert searched.status_code == 200, searched.text
    assert searched.json()["strategy_source"] == "depth_limited_search", searched.json()
    assert searched.json()["diagnostics"]["root_worlds"] == 6
    other = session(client, "leduc")
    unsupported = client.post(
        "/v1/analyze",
        json={
            **payload,
            "session_id": other["session_id"],
            "state_version": other["state_version"],
        },
    )
    assert unsupported.json()["status"] == "unsupported"
    assert unsupported.json()["actions"] == []
    client.post(
        f"/v1/sessions/{current['session_id']}/actions",
        json={
            "action": "check",
            "event_id": str(uuid4()),
            "expected_version": current["state_version"],
        },
    )
    assert client.post("/v1/analyze", json=payload).status_code == 409


def test_zero_complete_iteration_never_publishes_fake_blueprint(client):
    response = client.post("/v1/training", json={"max_nodes": 1})
    job = response.json()
    for _ in range(100):
        job = client.get(f"/v1/training/{job['job_id']}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.01)
    assert job["status"] == "completed"
    assert job["blueprint_id"] is None
    assert job["diagnostics"]["completed_iterations"] == 0
    assert client.get("/v1/blueprints").json() == {"blueprints": []}


def test_equity_is_share_not_strategy_and_rejects_duplicate_cards(client):
    payload = {"hero": ["2c", "3d"], "board": ["As", "Ks", "Qs", "Js", "Ts"], "samples": 20}
    response = client.post("/v1/equity", json=payload)
    assert response.status_code == 200, response.text
    contract("equity-response", response.json())
    assert response.json()["equity"] == 0.5
    assert response.json()["method"] == "exact_river"
    invalid = client.post("/v1/equity", json={**payload, "hero": ["As", "As"]})
    assert invalid.status_code == 422


def test_transaction_conflict_does_not_corrupt_replay(tmp_path):
    store = SessionStore(tmp_path / "store.sqlite3")
    session = store.create(CreateSession(game=GameConfig()))
    with pytest.raises(ValueError):
        store.act(
            session.session_id,
            SubmitAction(action="illegal", event_id="x", expected_version=session.state_version),
        )
    assert store.snapshot(session.session_id) == session
    store.act(
        session.session_id,
        SubmitAction(action="bet", event_id="x", expected_version=session.state_version),
    )
    with pytest.raises(ConflictError):
        store.act(
            session.session_id,
            SubmitAction(action="check", event_id="y", expected_version=session.state_version),
        )
    with store.connection() as db:
        events = db.execute("SELECT action FROM events ORDER BY seq").fetchall()
    assert len(events) == session.state_version + 1
    assert json.loads(store.snapshot(session.session_id).model_dump_json())["actor"] == 1


def test_profile_api_uses_requested_event_version_and_shared_contract(client):
    current = session(client)
    response = client.get(
        f"/v1/sessions/{current['session_id']}/profiles",
        params={
            "as_of": current["state_version"],
        },
    )
    assert response.status_code == 200, response.text
    contract("profiles-response", response.json())
    assert response.json()["state_version"] == current["state_version"]
    assert all(player["actions"] == [] for player in response.json()["players"])


def test_suit_canonical_simulator_preserves_rule_version_and_profile_api(client):
    result = client.post(
        "/v1/sessions",
        json={
            "game": {
                "game_id": "holdem",
                "num_players": 2,
                "abstraction": "suit_canonical",
            }
        },
    ).json()
    assert "suit-canonical-v1" in result["rules_version"]
    contract("session-response", result)
    response = client.get(f"/v1/sessions/{result['session_id']}/profiles")
    assert response.status_code == 200, response.text
    contract("profiles-response", response.json())


def test_hand_strength_distinguishes_theoretical_nuts_from_hero_blockers(client):
    response = client.post(
        "/v1/hand-strength",
        json={
            "hero": ["Ts", "Ah"],
            "board": ["6s", "7s", "8s", "9s"],
            "max_beating_combos": 5,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    contract("hand-strength-response", result)
    assert not result["nuts"]["theoretical"]["hero_is_nuts"]
    assert result["nuts"]["blocker_aware"]["hero_is_nuts"]
    assert result["equity_computed"] is False
    assert result["action_ev_computed"] is False
    preflop = client.post("/v1/hand-strength", json={"hero": ["As", "Ad"]}).json()
    assert preflop["current_rank"] is None
    assert preflop["nuts"] is None


def test_adaptation_experiment_reports_model_gain_and_explicit_risk_budget(client):
    job = train(client)
    payload = {
        "blueprint_id": job["blueprint_id"],
        "hero": 0,
        "opponent_mode": "call",
        "sample_count": 100,
        "max_worst_case_loss": 0.04,
    }
    response = client.post("/v1/experiments/adaptation", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    contract("adaptation-response", result)
    assert result["model_source"] == "synthetic_bias"
    experiment = result["result"]
    if experiment["accepted"]:
        assert experiment["model_gain"] > 0
        assert experiment["candidate_worst_case"] >= experiment["baseline_worst_case"] - 0.04 - 1e-9
    thin = client.post("/v1/experiments/adaptation", json={**payload, "sample_count": 0}).json()
    assert thin["result"]["accepted"] is False
    assert thin["result"]["selected_weight"] == 0
