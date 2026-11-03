"""Decision features must precede the action, including its early cash update."""

import copy
import hashlib
import json
import sqlite3

import pytest
from test_capture_learning import ORIGIN, PLAYERS, action, append, event

from texas_analysis import capture_learning
from texas_analysis.capture_contract import BusinessBatch
from texas_analysis.capture_learning import examples, initialize, profiles
from texas_analysis.network_stream import NetworkStore


def turn(player="player-2"):
    return {
        "player_id": player,
        "legal_actions": [],
        "call_amount": None,
        "min_raise_reported": None,
        "max_raise_reported": None,
    }


def snapshot(*, stack=930, player="player-2", street="preflop", board=None):
    players = copy.deepcopy(PLAYERS)
    next(p for p in players if p["player_id"] == "player-2")["stack"] = stack
    return {
        "street": street,
        "hero_cards": ["As", "Kd"],
        "board": board or [],
        "players": players,
        "turn": turn(player),
        "history_complete": True,
        "rules_version": "wpk-observed-unverified-v1",
    }


def decisions():
    label = action(player="player-2", action="RAISE", amount_to=34)
    label["amount_delta"] = 34
    return [
        event(1, "hand_started", {"hand_number": 1, "history_complete": True}),
        event(2, "players_updated", {"players": snapshot()["players"]}),
        event(3, "action_requested", {"turn": turn()}),
        event(4, "snapshot", snapshot()),
        event(5, "players_updated", {"players": snapshot(stack=896)["players"]}),
        event(6, "snapshot", snapshot(stack=896)),
        event(7, "action_confirmed", label),
    ]


@pytest.fixture
def db(tmp_path):
    with sqlite3.connect(tmp_path / "learning.sqlite3") as connection:
        initialize(connection)
        yield connection


def test_early_cash_update_and_later_snapshot_never_replace_decision_boundary(db):
    append(db, decisions())
    item = examples(db, ORIGIN, "source-1")["examples"][0]
    context = item["context"]
    assert context["available"] and context["history_complete"]
    assert context["observation_cutoff"] == "4"
    assert context["decision_request_sequence"] == "3"
    assert context["history_reference"] == {"hand_id": "table-1:1", "through_sequence": "4"}
    assert next(p["stack"] for p in context["players"] if p["player_id"] == "player-2") == 930
    assert "hero_cards" not in context
    assert item["observed_action"]["amount_delta"] == 34


def test_same_actor_repeated_request_does_not_refreeze_debited_cash(db):
    events = decisions()
    events[5] = event(6, "action_requested", {"turn": turn()})
    events.insert(6, event(7, "snapshot", snapshot(stack=896)))
    events[-1] = event(8, "action_confirmed", events[-1].data)
    append(db, events)
    assert examples(db, ORIGIN, "source-1")["examples"][0]["context"]["observation_cutoff"] == "4"


def test_repeated_request_cannot_repair_missing_first_snapshot(db):
    events = decisions()
    events[3] = event(4, "players_updated", {"players": snapshot(stack=896)["players"]})
    events[4] = event(5, "action_requested", {"turn": turn()})
    append(db, events)
    assert not examples(db, ORIGIN, "source-1")["examples"][0]["context"]["available"]


def test_board_before_turn_can_establish_new_street_without_stale_projection_label(db):
    events = decisions()
    flop = ["2c", "3d", "4h"]
    label = {**events[-1].data, "source_round": "FLOP"}
    append(
        db,
        events[:2]
        + [
            event(3, "board_updated", {"cards": flop}),
            event(4, "action_requested", {"turn": turn()}),
            event(5, "snapshot", snapshot(street="flop", board=flop)),
            event(6, "action_confirmed", label),
        ],
    )
    context = examples(db, ORIGIN, "source-1")["examples"][0]["context"]
    assert context["available"] and context["board"] == flop
    assert context["observation_cutoff"] == "5"


def test_hero_cards_learned_after_decision_are_not_backfilled(db):
    events = decisions()
    first = snapshot(player="hero")
    first["hero_cards"] = [None, None]
    append(
        db,
        events[:2]
        + [
            event(3, "action_requested", {"turn": turn("hero")}),
            event(4, "snapshot", first),
            event(5, "hero_cards", {"cards": ["As", "Kd"]}),
            event(6, "snapshot", snapshot(player="hero")),
            event(7, "action_confirmed", {**events[-1].data, "player_id": "hero"}),
        ],
    )
    context = examples(db, ORIGIN, "source-1")["examples"][0]["context"]
    assert context["available"] and context["hero_cards"] == [None, None]
    assert context["observation_cutoff"] == "4"


def test_earlier_action_id_cannot_use_later_decision_prefix(db):
    events = decisions()
    later = {**events[-1].data, "source_event_id": "3"}
    earlier = {**events[-1].data, "source_event_id": "2"}
    append(
        db,
        events[:2]
        + [
            event(3, "action_confirmed", later),
            event(4, "action_requested", {"turn": turn()}),
            event(5, "snapshot", snapshot()),
            event(6, "action_confirmed", earlier),
        ],
    )
    item = examples(db, ORIGIN, "source-1")["examples"][0]
    assert item["source_event_id"] == "2" and not item["context"]["available"]
    assert item["context"]["history_reference"] is None


@pytest.mark.parametrize(
    "case",
    ["no_request", "no_snapshot", "interposed_cash", "wrong_actor", "wrong_hand", "wrong_street"],
)
def test_unproven_boundary_keeps_counts_but_has_no_training_features(db, case):
    events = decisions()
    if case == "no_request":
        events[2] = event(3, "players_updated", {"players": snapshot()["players"]})
    elif case == "no_snapshot":
        events[3] = event(4, "players_updated", {"players": snapshot()["players"]})
    elif case == "interposed_cash":
        events[3] = event(4, "players_updated", {"players": snapshot(stack=896)["players"]})
    elif case == "wrong_actor":
        events[3] = event(4, "snapshot", snapshot(player="player-1"))
    elif case == "wrong_hand":
        events[3] = event(4, "snapshot", snapshot(), hand=2)
        events = events[:4] + [event(5, "action_confirmed", events[-1].data, hand=2)]
    else:
        events[3] = event(4, "snapshot", snapshot(street="flop"))
    append(db, events)
    context = examples(db, ORIGIN, "source-1")["examples"][0]["context"]
    assert not context["available"] and context["players"] == []
    assert context["history_reference"] is None
    assert profiles(db, ORIGIN, "source-1")["players"][0]["action_observations"] == 1


@pytest.mark.parametrize(
    "kind,data",
    [
        ("capture_gap", {"reason": "lost frame"}),
        ("room_closed", {"reconciled": False}),
        ("board_updated", {"cards": ["2c", "3d", "4h"]}),
        ("action_requested", {"turn": None}),
        ("action_requested", {"turn": turn("hero")}),
    ],
)
def test_decision_lifecycle_invalidates_old_anchor(db, kind, data):
    events = decisions()
    events[4] = event(5, kind, data)
    append(db, events[:5] + [events[-1]])
    assert not examples(db, ORIGIN, "source-1")["examples"][0]["context"]["available"]


def test_boundaries_are_scoped_and_survive_reopening(db):
    state = append(db, decisions()[:4])
    initialize(db)
    for origin, source in ((ORIGIN + "b", "source-1"), (ORIGIN, "source-2")):
        label = event(7, "action_confirmed", decisions()[-1].data, source=source)
        append(db, [label], state=copy.deepcopy(state), origin=origin)
        assert not examples(db, origin, source)["examples"][0]["context"]["available"]
    append(db, decisions()[4:], state=state)
    assert examples(db, ORIGIN, "source-1")["examples"][0]["context"]["available"]


def batch(events):
    return BusinessBatch.model_validate(
        {
            "schema": "tgto.capture/0.1",
            "type": "capture.append",
            "source_id": "source-1",
            "events": [e.model_dump(by_alias=True) for e in events],
        }
    )


def legacy_store(path):
    store = NetworkStore(path, stream="capture")
    store.append(ORIGIN, batch(decisions()))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE capture_learning_metadata SET value='1' WHERE key='schema_version'")
        context = json.loads(
            db.execute("SELECT context FROM capture_learning_actions").fetchone()[0]
        )
        context["players"] = snapshot(stack=896)["players"]
        context["observation_cutoff"] = "6"
        db.execute("UPDATE capture_learning_actions SET context=?", (json.dumps(context),))
    return store


def receipts(db):
    return hashlib.sha256(
        json.dumps(db.execute("SELECT * FROM capture_events ORDER BY sequence").fetchall()).encode()
    ).hexdigest()


def test_migration_rebuilds_old_equal_head_without_modifying_receipts(tmp_path):
    path = tmp_path / "capture.sqlite3"
    legacy_store(path)
    with sqlite3.connect(path) as db:
        old_receipts = receipts(db)
    NetworkStore(path, stream="capture")
    with sqlite3.connect(path) as db:
        item = examples(db, ORIGIN, "source-1")["examples"][0]
        assert item["context"]["observation_cutoff"] == "4"
        assert (
            next(p["stack"] for p in item["context"]["players"] if p["player_id"] == "player-2")
            == 930
        )
        assert receipts(db) == old_receipts
        assert db.execute("SELECT value FROM capture_learning_metadata").fetchone()[0] == "2"
        expected = profiles(db, ORIGIN, "source-1")
    NetworkStore(path, stream="capture")
    with sqlite3.connect(path) as db:
        assert profiles(db, ORIGIN, "source-1") == expected


def test_migration_failure_rolls_back_all_derived_data_and_version(tmp_path, monkeypatch):
    path = tmp_path / "capture.sqlite3"
    legacy_store(path)
    with sqlite3.connect(path) as db:
        previous = list(db.iterdump())
    original = capture_learning.observe

    def fail(db, origin, event, before, after):
        if event.sequence == "7":
            raise RuntimeError("rebuild failed")
        return original(db, origin, event, before, after)

    monkeypatch.setattr(capture_learning, "observe", fail)
    with pytest.raises(RuntimeError, match="rebuild failed"):
        NetworkStore(path, stream="capture")
    with sqlite3.connect(path) as db:
        assert list(db.iterdump()) == previous


def test_future_learning_version_is_rejected_without_reset(db):
    append(db, decisions())
    with db:
        db.execute("UPDATE capture_learning_metadata SET value='999'")
    previous = list(db.iterdump())
    with pytest.raises(ValueError, match="schema version"):
        initialize(db)
    assert list(db.iterdump()) == previous
