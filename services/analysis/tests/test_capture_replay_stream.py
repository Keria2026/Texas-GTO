"""Public replay diagnostics keep the existing extension's unsupported wire contract."""

from copy import deepcopy

from texas_analysis.capture_contract import BusinessBatch
from texas_analysis.network_stream import NetworkStore

ORIGIN = "chrome-extension://" + "a" * 32


def observed_opening():
    events = []

    def add(kind, data, hand=2):
        events.append(
            {
                "schema": "tgto.capture/0.1",
                "type": "business_event",
                "adapter_version": "wpk/1",
                "source_id": "synthetic-replay-stream",
                "sequence": str(len(events) + 1),
                "captured_at": "2026-11-02T12:00:00Z",
                "table_id": "table-1",
                "hand_id": f"table-1:{hand}",
                "kind": kind,
                "data": data,
            }
        )

    players = ["hero", "player-1", "player-2", "player-3"]
    add(
        "settlement",
        {
            "table_id": "table-1",
            "hand_number": 1,
            "status": "reconciled",
            "reason": None,
            "pool_change": 0,
            "hero_cards": ["Qs", "Jh"],
            "board": [],
            "players": [
                {
                    "player_id": p,
                    "mushroom_paid": 40 if p == "player-3" else 20,
                    "mushroom_awarded": 100 if p == "player-3" else 0,
                    "net_chips": 60 if p == "player-3" else -20,
                }
                for p in players
            ],
        },
        hand=1,
    )
    add("hand_started", {"hand_number": 2, "history_complete": True})
    for index, p in enumerate(players):
        add(
            "action_confirmed",
            {
                "source_event_id": str(2001 + index),
                "player_id": p,
                "action": "PAY_MUSHROOM",
                "amount_delta": 40 if index == 0 else 20,
                "amount_to": None,
                "source_round": "CLEAN",
            },
        )
    for index, (kind, amount) in enumerate(
        zip(("SMALL_BLIND", "BIG_BLIND", "THIRD_BLIND"), (2, 4, 8), strict=True)
    ):
        add(
            "action_confirmed",
            {
                "source_event_id": str(2005 + index),
                "player_id": players[index],
                "action": kind,
                "amount_delta": amount,
                "amount_to": amount,
                "source_round": "BET_BLIND",
            },
        )
    add(
        "snapshot",
        {
            "history_complete": True,
            "rules_version": "wpk-observed-unverified-v1",
            "street": "preflop",
            "hero_cards": ["As", "Kd"],
            "board": [],
            "players": [
                {"player_id": p, "seat": seat, "stack": cash}
                for p, seat, cash in zip(players, (1, 2, 4, 7), (758, 776, 772, 780), strict=True)
            ],
            "turn": {
                "player_id": "player-3",
                "legal_actions": [],
                "call_amount": None,
                "min_raise_reported": None,
                "max_raise_reported": None,
            },
        },
    )
    return events


def append(store, events):
    return store.append(
        ORIGIN,
        BusinessBatch.model_validate(
            {
                "schema": "tgto.capture/0.1",
                "type": "capture.append",
                "source_id": events[0]["source_id"],
                "events": events,
            }
        ),
    )


def test_verified_roster_cash_and_ledger_do_not_approve_a_strategy(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    events = observed_opening()
    append(store, events)
    result = store.state(ORIGIN, events[0]["source_id"])
    assert result["through_sequence"] == events[-1]["sequence"]
    assert result["analysis"] == {
        "status": "unsupported",
        "reason": "unverified_rules_and_strategy_coverage",
        "actions": [],
        "missing_fields": [
            "verified_rule_profile",
            "supported_strategy_model",
            "button_seat",
            "model_context_mapping",
        ],
    }
    assert store.state(ORIGIN + "b", events[0]["source_id"]) is None


def test_a_partial_batch_cannot_reuse_previous_public_verification(tmp_path):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    events = observed_opening()
    append(store, events)
    next_event = deepcopy(events[-1])
    next_event.update(sequence=str(len(events) + 1), kind="players_updated")
    next_event["data"] = {"players": deepcopy(events[-1]["data"]["players"])}
    next_event["data"]["players"][-1]["stack"] -= 16
    append(store, [next_event])
    result = store.state(ORIGIN, events[0]["source_id"])
    assert result["through_sequence"] == next_event["sequence"]
    assert {"hand_participants", "aligned_starting_stacks", "mushroom_ledger_anchor"} <= set(
        result["analysis"]["missing_fields"]
    )
    assert result["analysis"]["actions"] == []


def test_long_prefix_is_rejected_instead_of_truncated(tmp_path, monkeypatch):
    store = NetworkStore(tmp_path / "capture.sqlite3", stream="capture")
    events = observed_opening()
    append(store, events)
    monkeypatch.setattr("texas_analysis.network_stream.MAX_REPLAY_EVENTS", 5)
    result = store.state(ORIGIN, events[0]["source_id"])
    assert "aligned_starting_stacks" in result["analysis"]["missing_fields"]
    assert result["analysis"]["actions"] == []
