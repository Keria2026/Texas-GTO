"""Search integration over synthetic eight-seat events; no live quality claim."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_capture_recommendation import (
    ORIGIN,
    batch,
    call,
    eight_player_events,
    fixture_model,
    observation,
)

from texas_analysis import capture_recommendation as recommendations
from texas_analysis.app import create_app
from texas_analysis.capture_recommendation import prepare_query
from texas_strategy.evaluation import CancellationError, EvaluationLimitError
from texas_strategy.persistent_policy import PersistentPolicy
from texas_strategy.search import SearchCoverageError


def eight_player_flop_events():
    events = eight_player_events()
    _, replay, _ = prepare_query(observation(events), events)
    game, hand = replay.game, replay.state

    def add(kind, data):
        events.append({**events[-1], "sequence": str(len(events) + 1), "kind": kind, "data": data})

    def act(action, street):
        nonlocal hand
        player = game.actor(hand)
        amount = min(game.amount_to_call(hand), hand.stacks[player]) if action == "call" else 0
        add(
            "action_confirmed",
            {
                "source_event_id": str(1 + sum(e["kind"] == "action_confirmed" for e in events)),
                "player_id": replay.player_ids[player],
                "action": action.upper(),
                "amount_delta": amount,
                "amount_to": hand.street_bets[player] + amount if amount else None,
                "source_round": street,
            },
        )
        hand = game.transition(hand, action)

    def snapshot(street, turn):
        data = deepcopy(events[13]["data"])
        data.update(street=street, board=list(hand.board), turn=turn)
        for player, amount in zip(data["players"], hand.stacks, strict=True):
            player["stack"] = amount
        add("snapshot", data)

    while game.actor(hand) != -1:
        act("call" if game.amount_to_call(hand) else "check", "PRE_FLOP")
    snapshot("preflop", None)
    add("board_updated", {"cards": ["2c", "3d", "4h"]})
    for card in ("2c", "3d", "4h"):
        hand = game.transition(hand, f"deal:{card}")
    while replay.player_ids[game.actor(hand)] != "hero":
        act("check", "FLOP")
    snapshot(
        "flop",
        {
            "player_id": "hero",
            "legal_actions": ["FOLD", "CHECK", "BET", "ALL_IN"],
            "call_amount": 0,
            "min_raise_reported": None,
            "max_raise_reported": None,
        },
    )
    assert prepare_query(observation(events), events)[1].state == hand
    return events


def test_postflop_attempt_is_opt_in_and_missing_likelihood_never_becomes_uniform(tmp_path):
    events = eight_player_flop_events()
    path = tmp_path / "training.sqlite3"
    fixture_model(path, events)
    prior = path.read_bytes()
    legacy = call(events, path)
    result = call(events, path, search=True)
    assert legacy["status"] == result["status"] == "ready"
    assert "search" not in legacy["model"]
    assert result["model"]["source"] == "pluribus_blueprint"
    assert result["model"]["search"]["status"] == "coverage_missing"
    assert result["actions"] == legacy["actions"]
    assert result["selected_action"] == legacy["selected_action"]
    assert path.read_bytes() == prior


def test_preflop_uses_blueprint_without_search_even_when_negotiated(tmp_path, monkeypatch):
    events = eight_player_events()
    path = tmp_path / "training.sqlite3"
    fixture_model(path, events)
    monkeypatch.setattr(recommendations, "_search", lambda *a, **k: pytest.fail("preflop search"))
    result = call(events, path, search=True)
    assert result["status"] == "ready" and "search" not in result["model"]


def install_search(monkeypatch, *, failure=None, mismatch=False):
    seen = []

    class Root:
        def __init__(self, model, public, policy, *, budget):
            assert not hasattr(public, "hole_cards")
            self.model, self.policy, self.budget = model, policy, budget
            seen.append(self)

        def solve(self, hero, cards, **kwargs):
            assert hero == 4 and cards == ("As", "Kd")
            assert kwargs["iterations"] == 100 and kwargs["max_nodes"] == 30000
            assert self.budget.deadline_seconds <= recommendations.SEARCH_SECONDS
            self.policy._db.execute("SELECT document FROM training_meta").fetchone()
            if failure is not None:
                raise failure("synthetic failure")
            actions = ("check", "raise_min", "raise_half_pot", "raise_pot", "all_in")
            if mismatch:
                actions = ("fold",)
            return SimpleNamespace(
                actions=actions,
                probabilities=(1.0,) + (0.0,) * (len(actions) - 1),
                blueprint_fallback_queries=0,
                diagnostics=SimpleNamespace(
                    completed_iterations=10, nodes=80, stop_reason="node_budget"
                ),
                subgame=SimpleNamespace(rollout_budget=SimpleNamespace(nodes=40), root_draws=80),
            )

    monkeypatch.setattr(recommendations, "MushroomFirstHandRoot", Root)
    return seen


def test_search_replaces_probabilities_and_releases_its_pinned_snapshot(tmp_path, monkeypatch):
    events = eight_player_flop_events()
    path = tmp_path / "training.sqlite3"
    query = fixture_model(path, events)
    assert query.actions == ("check", "raise_min", "raise_half_pot", "raise_pot", "all_in")
    seen = install_search(monkeypatch)
    result = call(events, path, search=True)
    assert result["status"] == "ready", result
    assert result["reason"] == "searched_strategy"
    assert result["model"]["source"] == "pluribus_search"
    assert result["model"]["search"]["iterations"] == 10
    assert result["actions"][result["selected_action"]]["action"] == "CHECK"
    assert "local_search_not_equilibrium_certified" in result["warnings"]
    assert "latest_completed_search_policy" in result["warnings"]
    with pytest.raises(ValueError, match="closed"):
        seen[0].policy.lookup(0, "closed")


@pytest.mark.parametrize("failure", [SearchCoverageError, EvaluationLimitError, CancellationError])
def test_search_failure_only_allows_current_covered_baseline(tmp_path, monkeypatch, failure):
    events = eight_player_flop_events()
    path = tmp_path / "training.sqlite3"
    fixture_model(path, events)
    seen = install_search(monkeypatch, failure=failure)
    if failure is CancellationError:
        with pytest.raises(CancellationError):
            call(events, path, search=True)
    else:
        result = call(events, path, search=True)
        assert result["status"] == "ready" and result["model"]["source"] == "pluribus_blueprint"
        expected = "coverage_missing" if failure is SearchCoverageError else "budget"
        assert result["model"]["search"]["status"] == expected
    assert seen[0].policy._db is None
    # An uncovered current node cannot be used after a failed search.
    monkeypatch.setattr(PersistentPolicy, "lookup", lambda *_: None)
    if failure is not CancellationError:
        result = call(events, path, search=True)
        assert result["status"] == "unavailable" and result["actions"] == []


@pytest.mark.parametrize("failure", [None, SearchCoverageError, EvaluationLimitError])
def test_search_may_supply_feedback_but_cannot_fall_back_to_initialization(
    tmp_path, monkeypatch, failure
):
    events = eight_player_flop_events()
    path = tmp_path / "training.sqlite3"
    fixture_model(path, events, feedback=False)
    install_search(monkeypatch, failure=failure)
    result = call(events, path, search=True)
    if failure is None:
        assert result["status"] == "ready" and result["model"]["source"] == "pluribus_search"
    else:
        assert result["reason"] == "strategy_feedback_unavailable"
        assert result["status"] == "unavailable" and result["actions"] == []


def test_search_cannot_return_different_actions_or_hide_total_expiry(tmp_path, monkeypatch):
    events = eight_player_flop_events()
    path = tmp_path / "training.sqlite3"
    fixture_model(path, events)
    install_search(monkeypatch, mismatch=True)
    result = call(events, path, search=True)
    assert result["reason"] == "model_or_state_invalid" and result["actions"] == []
    seen = install_search(monkeypatch)
    monkeypatch.setattr(recommendations, "monotonic", lambda: 10.0 if seen else 0.0)
    result = call(events, path, search=True)
    assert result["reason"] == "query_timeout" and result["actions"] == []
    assert seen[0].policy._db is None


@pytest.mark.parametrize("negotiated", [False, True])
def test_websocket_only_sends_search_to_capable_clients(tmp_path, monkeypatch, negotiated):
    events = eight_player_flop_events()
    path = tmp_path / "core" / "training.sqlite3"
    path.parent.mkdir()
    fixture_model(path, events)
    seen = install_search(monkeypatch)
    with TestClient(create_app(data_dir=tmp_path, auto_train=False)) as client:
        with client.websocket_connect(
            "ws://127.0.0.1/v1/research/stream", headers={"origin": ORIGIN}
        ) as ws:
            ws.send_json(
                {
                    "type": "auth",
                    "capabilities": ["capture_recommendations_v1"]
                    + (["capture_search_v1"] if negotiated else []),
                }
            )
            assert "capture_search_v1" in ws.receive_json()["capabilities"]
            ws.send_json(batch(events).model_dump(by_alias=True))
            assert ws.receive_json()["type"] == "capture.ack"
            result = ws.receive_json()
            assert result["status"] == "ready", result
            assert result["model"]["source"] == (
                "pluribus_search" if negotiated else "pluribus_blueprint"
            )
            assert bool(seen) == negotiated
