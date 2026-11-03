"""Synthetic complete captures exercise extraction, automatic release and preservation."""

import json
from contextlib import closing

from test_capture_recommendation import ORIGIN, batch
from test_capture_replay import PLAYERS, add_action, add_event, add_snapshot, opening_events

from texas_analysis import capture_strategy
from texas_analysis.capture_replay import replay_capture
from texas_analysis.network_stream import NetworkStore


def completed_hand(hand, offset):
    events = opening_events(hand=hand)
    replay = replay_capture(events)
    game, state = replay.game, replay.state
    add_event(events, "action_requested", {"turn": events[-1]["data"]["turn"]})
    add_snapshot(events, state.stacks, actor="hero", call=8)
    add_action(events, "hero", "RAISE", 16, total=16)
    state = game.transition(state, "raise_to:16")
    for player in PLAYERS[1:]:
        turn = {
            "player_id": player,
            "legal_actions": [],
            "call_amount": None,
            "min_raise_reported": None,
            "max_raise_reported": None,
        }
        add_event(events, "action_requested", {"turn": turn})
        add_snapshot(events, state.stacks, actor=player)
        add_action(events, player, "FOLD", 0)
        state = game.transition(state, "fold")
    add_snapshot(events, state.stacks)
    add_event(
        events,
        "settlement",
        {
            "table_id": "table-1",
            "hand_number": hand,
            "status": "reconciled",
            "reason": None,
            "pool_change": 100,
            "hero_cards": ["As", "Kd"],
            "board": [],
            "players": [
                {"player_id": p, "net_chips": n, "mushroom_paid": fee, "mushroom_awarded": 0}
                for p, n, fee in zip(PLAYERS, (-6, -42, -24, -28), (20, 40, 20, 20), strict=True)
            ],
        },
    )
    for event in events:
        event["sequence"] = str(int(event["sequence"]) + offset)
    return events


def test_capture_to_conditional_strategy_to_evaluation_release_and_restart(tmp_path):
    path = tmp_path / "capture.sqlite3"
    store = NetworkStore(path, stream="capture")
    total = 0
    for hand in range(1, 31):
        events = completed_hand(hand, total)
        store.append(ORIGIN, batch(events))
        total += len(events)
    with closing(store.connect()) as db:
        data, excluded, _ = capture_strategy.dataset(db, ORIGIN)
        assert len(data) == 90 and excluded == {}
        assert all(s["coarse"][0] == 0 and s["label"] == "fold" for s in data)
        assert all(s["player"] != "hero" and "hero_cards" not in s for s in data)
        original = db.execute("SELECT SUM(length(body)),COUNT(*) FROM capture_events").fetchone()
    store.train_behavior()
    first = store.learning_status(ORIGIN)
    assert first["last_run"]["status"] == "promoted"
    assert first["last_run"]["test_samples"] >= 20
    assert store.learning_status("another-origin")["active_version"] is None
    resumed = NetworkStore(path, stream="capture")
    resumed.train_behavior()
    assert resumed.learning_status(ORIGIN) == first
    with closing(resumed.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM capture_strategy_runs").fetchone()[0] == 1
        assert (
            db.execute("SELECT SUM(length(body)),COUNT(*) FROM capture_events").fetchone()
            == original
        )
        model = capture_strategy.load(db, ORIGIN)
        assert model["available_after"] == total
        assert model["dataset_digest"] and model["algorithm"] == capture_strategy.VERSION
        assert "hero_cards" not in json.dumps(model)
