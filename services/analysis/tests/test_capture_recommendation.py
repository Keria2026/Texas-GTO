"""Synthetic eight-seat wire fixtures; never labelled real WPK acceptance."""

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from texas_analysis.app import create_app
from texas_analysis.capture_contract import BusinessBatch, BusinessEvent
from texas_analysis.capture_recommendation import prepare_query, recommend
from texas_analysis.capture_state import reduce_event
from texas_analysis.network_stream import NetworkStore
from texas_strategy.cfr import _TableEntry
from texas_strategy.training_store import open_trainer

ORIGIN = "chrome-extension://" + "a" * 32


def eight_player_events(*, button=0, now=None):
    now = now or datetime.now(UTC).isoformat()
    hero = (button + 4) % 8
    players = ["hero" if p == hero else f"player-{p + 1}" for p in range(8)]
    events = []

    def add(kind, data, hand=2):
        events.append(
            {
                "schema": "tgto.capture/0.1",
                "type": "business_event",
                "adapter_version": "wpk/1",
                "source_id": "synthetic-eight",
                "sequence": str(len(events) + 1),
                "captured_at": now,
                "kind": kind,
                "table_id": "table-1",
                "hand_id": f"table-1:{hand}",
                "data": data,
            }
        )

    add(
        "settlement",
        {
            "table_id": "table-1",
            "hand_number": 1,
            "status": "reconciled",
            "reason": None,
            "pool_change": 0,
            "hero_cards": ["As", "Kd"],
            "board": [],
            "players": [
                {
                    "player_id": p,
                    "mushroom_paid": 40 if i == button else 20,
                    "mushroom_awarded": 180 if i == button else 0,
                    "net_chips": 140 if i == button else -20,
                }
                for i, p in enumerate(players)
            ],
        },
        hand=1,
    )
    add("hand_started", {"hand_number": 2, "history_complete": True})
    fees = [40 if i == (button + 1) % 8 else 20 for i in range(8)]
    forced = [0] * 8
    action_id = 0
    for i, p in enumerate(players):
        action_id += 1
        add(
            "action_confirmed",
            {
                "source_event_id": str(action_id),
                "player_id": p,
                "action": "PAY_MUSHROOM",
                "amount_delta": fees[i],
                "amount_to": None,
                "source_round": "CLEAN",
            },
        )
    for step, (action, amount) in enumerate(
        zip(("SMALL_BLIND", "BIG_BLIND", "THIRD_BLIND"), (2, 4, 8), strict=True), 1
    ):
        seat = (button + step) % 8
        forced[seat] = amount
        action_id += 1
        add(
            "action_confirmed",
            {
                "source_event_id": str(action_id),
                "player_id": players[seat],
                "action": action,
                "amount_delta": amount,
                "amount_to": amount,
                "source_round": "BET_BLIND",
            },
        )
    add(
        "snapshot",
        {
            "street": "preflop",
            "hero_cards": ["As", "Kd"],
            "board": [],
            "players": [
                {"player_id": p, "seat": i + 1, "stack": 800 - fees[i] - forced[i]}
                for i, p in enumerate(players)
            ],
            "turn": {
                "player_id": "hero",
                "legal_actions": ["FOLD", "CALL", "RAISE", "ALL_IN"],
                "call_amount": 8,
                "min_raise_reported": None,
                "max_raise_reported": None,
            },
            "history_complete": True,
            "rules_version": "wpk-observed-unverified-v1",
        },
    )
    return events


def observation(events):
    state = None
    for event in events:
        state = reduce_event(state, BusinessEvent.model_validate(event))
    return state


def batch(events):
    return BusinessBatch.model_validate(
        {
            "schema": "tgto.capture/0.1",
            "type": "capture.append",
            "source_id": events[0]["source_id"],
            "events": events,
        }
    )


def fixture_model(path, events, *, average=True):
    """Insert an explicitly synthetic weighted policy to test transport, not strength."""
    state = observation(events)
    game, _, query = prepare_query(state, events)
    trainer = open_trainer(game, path)
    try:
        trainer._store.begin(0)
        trainer._store.set_entry(
            (query.player, query.information_key),
            _TableEntry(
                query.actions,
                [99.0] * len(query.actions),
                list(range(1, len(query.actions) + 1)) if average else [0.0] * len(query.actions),
            ),
            0,
        )
        trainer._store.commit(trainer, 1)
    finally:
        trainer._store.close()
    return query


def call(events, path, **kwargs):
    return recommend(
        source=events[0]["source_id"],
        through=events[-1]["sequence"],
        state=observation(events),
        prefix=events,
        policy_path=path,
        **kwargs,
    )


def test_eight_seat_rotation_keeps_query_and_actual_amounts(tmp_path):
    events = eight_player_events()
    path = tmp_path / "training.sqlite3"
    original = fixture_model(path, events)
    for button in range(8):
        events = eight_player_events(button=button)
        query = prepare_query(observation(events), events)[2]
        assert (query.player, query.information_key) == (original.player, original.information_key)
        result = call(events, path)
        assert result["status"] == "ready", result
        assert result["model"]["source"] == "pluribus_blueprint"
        assert result["model"]["opponent_adaptation"] is False
        assert sum(a["probability"] for a in result["actions"]) == pytest.approx(1)
        assert next(a for a in result["actions"] if a["action"] == "CALL")["amount_delta"] == 8
        assert next(a for a in result["actions"] if a["label"] == "raise_min")["raise_to"] == 16
        assert next(a for a in result["actions"] if a["label"] == "all_in")["amount_delta"] == 780
        assert result["selected_action"] == call(events, path)["selected_action"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("stale", "observation_expired"),
        ("gap", "incomplete_action_history"),
        ("turn", "not_hero_turn"),
        ("cards", "hero_cards_unavailable"),
        ("ledger", "mushroom_ledger_unavailable"),
        ("options", "client_legal_actions_conflict"),
        ("cash", "public_state_mismatch"),
    ],
)
def test_invalid_facts_clear_every_action(tmp_path, change, reason):
    events = eight_player_events()
    path = tmp_path / "training.sqlite3"
    fixture_model(path, events)
    state = observation(events)
    if change == "stale":
        events[-1]["captured_at"] = "2020-01-01T00:00:00Z"
    elif change == "gap":
        state["gap"] = True
    elif change == "turn":
        state["turn"] = None
    elif change == "cards":
        state["hero_cards"] = [None, None]
    elif change == "ledger":
        state["settlements"] = []
    elif change == "options":
        state["turn"]["legal_actions"] = ["FOLD"]
        events[-1]["data"]["turn"]["legal_actions"] = ["FOLD"]
    else:
        events[-1]["data"]["players"][4]["stack"] -= 20
    result = recommend(
        source="synthetic-eight",
        through=events[-1]["sequence"],
        state=state,
        prefix=events,
        policy_path=path,
    )
    assert result["reason"] == reason, result
    assert result["status"] == "unavailable" and result["actions"] == []
    assert result["selected_action"] is None and result["model"] is None


def test_missing_average_never_uses_regrets_or_uniform(tmp_path):
    events = eight_player_events()
    path = tmp_path / "training.sqlite3"
    fixture_model(path, events, average=False)
    result = call(events, path)
    assert result["reason"] == "strategy_not_trained" and result["actions"] == []


def test_backend_rechecks_head_after_model_read(tmp_path, monkeypatch):
    events = eight_player_events()
    path = tmp_path / "training.sqlite3"
    fixture_model(path, events)
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture", policy_path=path)
    store.append(ORIGIN, batch(events))
    assert store.recommendation(ORIGIN, "synthetic-eight")["status"] == "ready"
    assert store.recommendation(ORIGIN + "b", "synthetic-eight") is None
    original = recommend

    def change(**kwargs):
        result = original(**kwargs)
        event = deepcopy(events[-1])
        event.update(sequence=str(len(events) + 1), kind="capture_gap", data={"reason": "test"})
        store.append(ORIGIN, batch([event]))
        return result

    monkeypatch.setattr("texas_analysis.network_stream.recommend", change)
    assert store.recommendation(ORIGIN, "synthetic-eight") is None


@pytest.mark.parametrize("negotiated", [True, False])
def test_websocket_negotiates_recommendation_after_durable_ack(tmp_path, negotiated):
    events = eight_player_events()
    model = tmp_path / "core" / "training.sqlite3"
    model.parent.mkdir()
    fixture_model(model, events)
    with TestClient(
        create_app(data_dir=tmp_path, auto_train=False), base_url="http://127.0.0.1"
    ) as client:
        with client.websocket_connect(
            "ws://127.0.0.1/v1/research/stream", headers={"origin": ORIGIN}
        ) as ws:
            ws.send_json(
                {
                    "type": "auth",
                    "capabilities": ["capture_state_v1"]
                    + (["capture_recommendations_v1"] if negotiated else []),
                }
            )
            assert "capture_recommendations_v1" in ws.receive_json()["capabilities"]
            ws.send_json(batch(events).model_dump(by_alias=True))
            assert ws.receive_json()["type"] == "capture.ack"
            assert ws.receive_json()["type"] == "capture.state"
            if negotiated:
                result = ws.receive_json()
                assert result["type"] == "capture.recommendation" and result["status"] == "ready"
                assert result["through_sequence"] == events[-1]["sequence"]
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
