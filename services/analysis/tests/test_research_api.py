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


def train(client, algorithm="cfr", iterations=100):
    response = client.post(
        "/v1/training",
        json={"algorithm": algorithm, "iterations": iterations, "deadline_seconds": 5.0},
    )
    assert response.status_code == 200, response.text
    job = response.json()
    deadline = time.monotonic() + 10
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
        job = client.get(f"/v1/training/{job['job_id']}").json()
    assert job["status"] == "completed", job
    assert job["diagnostics"]["completed_iterations"] == iterations
    assert job["evaluation"]["quality"] == "exact_finite_game"
    assert job["evaluation"]["exploitability"] < 0.1
    contract("training-job", job)
    return job


def test_pluribus_training_blueprint_and_node_analysis_are_available_via_api(client):
    job = train(client, algorithm="pluribus", iterations=3000)
    assert job["diagnostics"]["discounted_iterations"] > 0
    blueprints = client.get("/v1/blueprints").json()["blueprints"]
    saved = next(b for b in blueprints if b["blueprint_id"] == job["blueprint_id"])
    assert saved["metadata"]["algorithm_version"] == "pluribus-iteration-schedule-v1"
    current = session(client)
    response = client.post(
        "/v1/analyze",
        json={
            "session_id": current["session_id"],
            "state_version": current["state_version"],
            "blueprint_id": job["blueprint_id"],
        },
    )
    assert response.status_code == 200
    contract("analysis-response", response.json())
    assert response.json()["status"] == "ready"


@pytest.mark.parametrize(
    "payload",
    [
        {"pluribus": {"prune_after": True}},
        {"pluribus": {"discount_interval": 0}},
        {"pluribus": {"unknown": 1}},
        {"weighting": "uniform"},
    ],
)
def test_pluribus_api_rejects_invalid_configuration(client, payload):
    response = client.post("/v1/training", json={"algorithm": "pluribus", **payload})
    assert response.status_code == 422


def test_eight_player_straddle_session_variable_stacks_and_last_seat_viewer(client):
    stacks = [800, 780, 900, 760, 880, 840, 800, 1000]
    response = client.post(
        "/v1/sessions",
        json={
            "game": {
                "game_id": "holdem",
                "num_players": 8,
                "stacks": stacks,
                "small_blind": 2,
                "big_blind": 4,
                "straddle": 8,
                "button": 3,
            }
        },
    )
    assert response.status_code == 200, response.text
    current = response.json()
    contract("session-response", current)
    assert current["actor"] == 7
    assert current["observation"]["straddlePlayer"] == 6
    assert current["observation"]["pot"] == 14
    actual = current["observation"]["players"]
    assert [p["stack"] + p["committed"] for p in actual] == stacks
    assert current["observation"]["betting"]["minimumRaiseTo"] == 16
    seen = client.get(f"/v1/sessions/{current['session_id']}?viewer=7")
    assert seen.status_code == 200
    contract("session-response", seen.json())
    assert seen.json()["observation"]["hero"] == 7
    profiles = client.get(f"/v1/sessions/{current['session_id']}/profiles")
    assert profiles.status_code == 200
    contract("profiles-response", profiles.json())
    assert len(profiles.json()["players"]) == 8


@pytest.mark.parametrize(
    "game",
    [
        {"game_id": "kuhn", "num_players": 8},
        {"game_id": "leduc", "num_players": 8},
        {"game_id": "kuhn", "straddle": 8},
        {"game_id": "holdem", "num_players": 8, "straddle": 2},
        {"game_id": "holdem", "num_players": 8, "stack": 7, "straddle": 8},
        {"game_id": "holdem", "num_players": 8, "stacks": [800] * 7},
        {"game_id": "holdem", "num_players": 8, "stacks": [800] * 7 + [0]},
        {"game_id": "holdem", "num_players": 8, "stacks": [800] * 7 + [True]},
    ],
)
def test_eight_seat_configuration_rejects_wrong_game_or_unsupported_forced_bets(client, game):
    assert client.post("/v1/sessions", json={"game": game}).status_code == 422


def test_eight_player_straddle_pluribus_training_via_api(client):
    response = client.post(
        "/v1/training",
        json={
            "game": {
                "game_id": "holdem",
                "num_players": 8,
                "stack": 8,
                "small_blind": 2,
                "big_blind": 4,
                "straddle": 8,
            },
            "algorithm": "pluribus",
            "iterations": 1,
            "seed": 42,
        },
    )
    assert response.status_code == 200, response.text
    job = response.json()
    deadline = time.monotonic() + 10
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
        job = client.get(f"/v1/training/{job['job_id']}").json()
    assert job["status"] == "completed", job
    assert job["diagnostics"]["completed_iterations"] == 1
    assert job["blueprint_id"] is not None
    assert job["evaluation"]["quality"] == "not_evaluated"


def test_eight_player_equity_accepts_seven_explicit_opponent_ranges(client):
    response = client.post(
        "/v1/equity",
        json={
            "hero": ["9c", "9d"],
            "board": ["As", "Ks", "Qs", "Js", "Ts"],
            "opponents": 7,
            "samples": 2,
            "ranges": [{f"{rank}c{rank}d": 1} for rank in "2345678"],
        },
    )
    assert response.status_code == 200, response.text
    contract("equity-response", response.json())
    assert response.json()["equity"] == pytest.approx(1 / 8)


def test_mushroom_cross_hand_model_trains_and_reports_its_finite_horizon(client):
    game = {
        "game_id": "mushroom",
        "num_players": 8,
        "stack": 800,
        "small_blind": 2,
        "big_blind": 4,
        "straddle": 8,
        "session_hands": 2,
    }
    created = client.post("/v1/sessions", json={"game": game, "seed": 42})
    assert created.status_code == 200, created.text
    current = created.json()
    contract("session-response", current)
    assert current["observation"]["mushroomPool"] == 180
    assert current["observation"]["trainingHorizon"] == 2
    assert current["observation"]["quality"] == "finite_horizon_research_model"
    response = client.post(
        "/v1/training",
        json={
            "game": game,
            "algorithm": "pluribus",
            "iterations": 1,
            "seed": 42,
            "deadline_seconds": 20.0,
        },
    )
    assert response.status_code == 200, response.text
    job = response.json()
    deadline = time.monotonic() + 30
    while job["status"] in ("queued", "running"):
        assert time.monotonic() < deadline
        time.sleep(0.01)
        job = client.get(f"/v1/training/{job['job_id']}").json()
    assert job["status"] == "completed", job
    assert job["diagnostics"]["completed_iterations"] == 1
    assert job["evaluation"]["quality"] == "not_evaluated"
    saved = client.get("/v1/blueprints").json()["blueprints"]
    blueprint = next(item for item in saved if item["blueprint_id"] == job["blueprint_id"])
    assert blueprint["metadata"]["pluribus"]["strategy_interval"] == 1
    assert blueprint["metadata"]["pluribus"]["average_mode"] == "sampled_mixture"
    assert blueprint["metadata"]["pluribus"]["average_samples"] == 16
    assert blueprint["metadata"]["game_id"] == "mushroom"


@pytest.mark.parametrize(
    "changes",
    [
        {"small_blind": 1},
        {"big_blind": 8},
        {"straddle": 16},
        {"num_players": 3},
        {"session_hands": 0},
        {"session_hands": True},
        {"abstraction": "suit_canonical"},
    ],
)
def test_mushroom_training_model_rejects_unimplemented_rule_variants(client, changes):
    game = {
        "game_id": "mushroom",
        "num_players": 8,
        "stack": 800,
        "small_blind": 2,
        "big_blind": 4,
        "straddle": 8,
        **changes,
    }
    assert client.post("/v1/sessions", json={"game": game}).status_code == 422


def test_context_blueprint_api_matches_nearby_stacks_and_returns_real_wagers(client, tmp_path):
    from texas_strategy.blueprint import Blueprint
    from texas_strategy.cfr import Trainer
    from texas_strategy.policy import PolicyEntry, TabularPolicy

    config = GameConfig(
        game_id="mushroom",
        num_players=8,
        stack=800,
        small_blind=2,
        big_blind=4,
        straddle=8,
        button=3,
        abstraction="mushroom_context",
    )
    original = client.post("/v1/sessions", json={"game": config.model_dump(), "seed": 42}).json()
    store = SessionStore(tmp_path / "sessions.sqlite3")
    _, _, game, state, _ = store.load(original["session_id"])
    actions = game.legal_actions(state)
    # A synthetic fixture checks lookup/translation, not trained strategy quality.
    metadata = {**Blueprint.from_trainer(Trainer(game)).metadata, "iterations": 1}
    blueprint = Blueprint(
        metadata,
        TabularPolicy(
            {
                (game.actor(state), game.information_key(state, game.actor(state))): PolicyEntry(
                    actions, tuple(float(a == "all_in") for a in actions)
                )
            }
        ),
    )
    blueprint_id = str(uuid4())
    blueprint.save(tmp_path / "models" / blueprint_id / "blueprint.json")
    for stack, expected_status in ((801, "ready"), (400, "unsupported")):
        current = client.post(
            "/v1/sessions",
            json={"game": {**config.model_dump(), "stack": stack}, "seed": 42},
        ).json()
        contract("session-response", current)
        result = client.post(
            "/v1/analyze",
            json={
                "session_id": current["session_id"],
                "state_version": current["state_version"],
                "blueprint_id": blueprint_id,
                "search": True,
            },
        )
        assert result.status_code == 200, result.text
        analyzed = result.json()
        contract("analysis-response", analyzed)
        assert analyzed["status"] == expected_status
        if expected_status == "ready":
            assert any("Approximate" in warning for warning in analyzed["warnings"])
            recommended = next(a for a in analyzed["actions"] if a["frequency"] == 1)
            assert recommended["action"] == "raise_to:781"
            actual = client.post(
                f"/v1/sessions/{current['session_id']}/actions",
                json={
                    "expected_version": current["state_version"],
                    "event_id": "recommended-action",
                    "action": recommended["action"],
                },
            )
            assert actual.status_code == 200, actual.text
            replayed = store.snapshot(current["session_id"])
            assert replayed.model_dump() == actual.json()
        else:
            assert analyzed["actions"] == []
            assert "information_set_not_trained" in analyzed["warnings"]


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
