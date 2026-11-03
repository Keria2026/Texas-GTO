import pytest
from fastapi.testclient import TestClient

from texas_analysis.app import create_app
from texas_analysis.training_monitor import (
    TrainingMonitor,
    _sample_public_case,
    public_facts,
    simulate,
)
from texas_strategy.cfr import _TableEntry
from texas_strategy.mushroom_population import MushroomPopulationGame
from texas_strategy.mushroom_public import build_public_query
from texas_strategy.training_store import open_trainer


@pytest.fixture
def model(tmp_path):
    game = MushroomPopulationGame(
        [dict(weight=1, stacks=[800] * 8, outstanding=[0] * 8, waiting_for_top_up=[], button=0)],
        ante=4,
    )
    path = tmp_path / "core" / "training.sqlite3"
    path.parent.mkdir()
    trainer = open_trainer(game, path)
    trainer._store.close()
    return game, path


def test_probe_is_public_deterministic_and_never_fabricates_or_trains(model):
    game, path = model
    first = simulate(path, seed=4, street=0, search=False)
    assert first["status"] == "unavailable" and not first["actions"]
    context, state, _ = _sample_public_case(game, 4, 0)
    facts = public_facts(context, state)
    assert not hasattr(facts["public_state"], "hole_cards")
    query = build_public_query(**facts)
    assert query.information_key == context.information_key(state, context.actor(state))
    trainer = open_trainer(game, path)
    trainer._store.begin(0)
    n = len(query.actions)
    trainer._store.set_entry(
        (query.player, query.information_key), _TableEntry(query.actions, [0.0] * n, [1.0] * n), 0
    )
    trainer._store.commit(trainer, 1)
    trainer._store.close()
    assert (
        simulate(path, seed=4, street=0, search=False)["reason"] == "strategy_feedback_unavailable"
    )
    trainer = open_trainer(game, path)
    trainer._store.begin(1)
    trainer._store.set_entry(
        (query.player, query.information_key),
        _TableEntry(query.actions, [1.0] + [0.0] * (n - 1), [1.0] * n),
        0,
    )
    trainer._store.commit(trainer, 2)
    before = trainer._store._document
    trainer._store.close()
    result = simulate(path, seed=4, street=0, search=True)
    assert result["status"] == "ready" and result["blueprint_hit"]
    assert result["hero_cards"] == first["hero_cards"]
    assert sum(a["probability"] for a in result["actions"]) == pytest.approx(1)
    assert result["search"]["status"] in ("coverage_missing", "budget")
    trainer = open_trainer(game, path)
    assert trainer._store._document == before
    trainer._store.close()
    status = TrainingMonitor(path).status()
    assert status["information_sets"] == status["averaged_information_sets"] == 1
    assert status["next_snapshot"] == 100


def test_dashboard_assets_and_api_boundary(model):
    _, path = model
    with TestClient(
        create_app(data_dir=path.parent.parent, auto_train=False), base_url="http://127.0.0.1:8765"
    ) as client:
        assert client.get("/training").status_code == 200
        assert "frame-ancestors" in client.get("/training").headers["content-security-policy"]
        for asset in ("monitor.js", "monitor.css"):
            assert client.get(f"/training/{asset}").status_code == 200
        assert client.get("/v1/training-monitor").status_code == 403
        headers = {"X-Texas-Local": "1", "Origin": "http://127.0.0.1:8765"}
        assert client.get("/v1/training-monitor", headers=headers).json()["model"]["available"]
        assert (
            client.get(
                "/v1/training-monitor", headers={**headers, "Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        response = client.post(
            "/v1/training-monitor/simulate",
            headers=headers,
            json={"seed": 4, "street": 0, "search": False},
        )
        assert response.status_code == 200 and response.json()["synthetic"]
        assert (
            client.post(
                "/v1/training-monitor/simulate", headers=headers, json={"seed": True}
            ).status_code
            == 422
        )


@pytest.mark.parametrize("street", [0, 1, 2, 3])
def test_generated_cases_reach_requested_street_with_matching_public_key(model, street):
    game, _ = model
    context, state, _ = _sample_public_case(game, 21, street)
    facts = public_facts(context, state)
    query = build_public_query(**facts)
    assert facts["public_state"].street == street
    assert query.information_key == context.information_key(state, context.actor(state))
