"""Observed behavior updates cannot duplicate people/actions or see future data."""

import copy
import json
import sqlite3

import pytest

from texas_analysis.capture_contract import BusinessEvent
from texas_analysis.capture_learning import examples, initialize, observe, profiles
from texas_analysis.capture_state import reduce_event

ORIGIN = "chrome-extension://" + "a" * 32
PLAYERS = [
    {"player_id": player, "seat": seat, "stack": 800}
    for seat, player in enumerate(("hero", "player-1", "player-2", "player-3"), 1)
]


def event(sequence, kind, data, *, source="source-1", hand=1):
    return BusinessEvent.model_validate(
        {
            "schema": "tgto.capture/0.1",
            "type": "business_event",
            "adapter_version": "wpk/1",
            "source_id": source,
            "sequence": str(sequence),
            "captured_at": "2026-11-01T00:00:00Z",
            "kind": kind,
            "table_id": "table-1",
            "hand_id": f"table-1:{hand}",
            "data": data,
        }
    )


def action(*, player="hero", action="CALL", round="PRE_FLOP", amount_to=None):
    return {
        "source_event_id": "1",
        "player_id": player,
        "action": action,
        "amount_delta": 4 if action == "CALL" else 0,
        "amount_to": amount_to,
        "source_round": round,
    }


def opening(*, source="source-1"):
    return [
        event(1, "hand_started", {"hand_number": 1, "history_complete": True}, source=source),
        event(
            2,
            "snapshot",
            {
                "street": "preflop",
                "hero_cards": ["As", "Kd"],
                "board": [],
                "players": PLAYERS,
                "turn": None,
                "history_complete": True,
                "rules_version": "wpk-observed-unverified-v1",
            },
            source=source,
        ),
    ]


def decision(sequence, *, source="source-1", hand=1, player="hero"):
    turn = {
        "player_id": player,
        "legal_actions": [],
        "call_amount": None,
        "min_raise_reported": None,
        "max_raise_reported": None,
    }
    snapshot = copy.deepcopy(opening()[1].data)
    snapshot["turn"] = turn
    return [
        event(sequence, "action_requested", {"turn": turn}, source=source, hand=hand),
        event(sequence + 1, "snapshot", snapshot, source=source, hand=hand),
    ]


def settlement(sequence, *, hand=1):
    return event(
        sequence,
        "settlement",
        {
            "table_id": "table-1",
            "hand_number": hand,
            "status": "reconciled",
            "reason": None,
            "pool_change": 0,
            "hero_cards": ["As", "Kd"],
            "board": ["2c", "3d", "4h", "8s", "9c"],
            "players": [
                {
                    "player_id": p["player_id"],
                    "net_chips": 0,
                    "mushroom_paid": 0,
                    "mushroom_awarded": 0,
                }
                for p in PLAYERS
            ],
        },
        hand=hand,
    )


def append(db, events, *, state=None, origin=ORIGIN):
    with db:
        for e in events:
            after = reduce_event(state, e)
            observe(db, origin, e, state, after)
            state = after
    return state


@pytest.fixture
def db(tmp_path):
    connection = sqlite3.connect(tmp_path / "learning.sqlite3")
    initialize(connection)
    yield connection
    connection.close()


@pytest.mark.parametrize("initial_round", [None, "PRE_FLOP"])
def test_action_updates_freeze_context_and_preserve_historical_availability(db, initial_round):
    events = opening() + decision(3) + [event(5, "action_confirmed", action(round=initial_round))]
    state = append(db, events)
    first = examples(db, ORIGIN, "source-1")["examples"][0]
    assert first["quality"] == ("complete_observed_history" if initial_round else "unknown_street")
    assert first["context"]["board"] == []
    assert "previous_actions" not in first["context"]
    assert first["context"]["history_reference"] == (
        {"hand_id": "table-1:1", "through_sequence": "4"} if initial_round else None
    )
    assert first["context"]["observation_cutoff"] == "4"
    later = [
        event(6, "board_updated", {"cards": ["2c", "3d", "4h"]}),
        event(7, "action_updated", action(amount_to=4)),
    ]
    append(db, later, state=state)
    current = examples(db, ORIGIN, "source-1")["examples"][0]
    assert current["context"] == first["context"]
    assert current["observed_sequence"] == "5" and current["available_sequence"] == "7"
    assert current["observed_action"]["amount_to"] == 4
    assert current["quality"] == (
        "complete_observed_history" if initial_round else "incomplete_history"
    )
    assert current["context"]["available"] == bool(initial_round)
    assert current["context"]["hero_cards"] == (["As", "Kd"] if initial_round else [None, None])
    historical = examples(db, ORIGIN, "source-1", as_of=6)["examples"][0]
    assert historical == first
    first_street = "preflop" if initial_round else "unknown"
    for cutoff, street in ((5, first_street), (6, first_street), (7, "preflop")):
        profile = profiles(db, ORIGIN, "source-1", as_of=cutoff)["players"][0]
        assert profile["action_observations"] == 1
        assert len(profile["contexts"]) == 1
        context = profile["contexts"][0]
        assert context["street"] == street and context["counts"]["CALL"] == 1
        assert context["posterior_probabilities"]["CALL"] == pytest.approx(2 / 7)
        assert sum(context["posterior_probabilities"].values()) == pytest.approx(1)


def test_replay_restart_and_duplicate_confirmation_do_not_increment_again(db, tmp_path):
    events = opening() + [event(3, "action_confirmed", action())]
    state = append(db, events)
    expected = profiles(db, ORIGIN, "source-1")
    append(db, events)
    assert profiles(db, ORIGIN, "source-1") == expected
    append(db, [event(4, "action_confirmed", action())], state=state)
    with sqlite3.connect(tmp_path / "learning.sqlite3") as restored:
        initialize(restored)
        current = profiles(restored, ORIGIN, "source-1")
        assert current["through_sequence"] == "4"
        assert current["players"] == expected["players"]
        assert len(examples(restored, ORIGIN, "source-1")["examples"]) == 1


def test_identity_scope_and_opponent_context_never_include_hero_private_cards(db):
    for origin, source, chosen in (
        (ORIGIN, "one", "FOLD"),
        (ORIGIN, "two", "CHECK"),
        (ORIGIN + "b", "one", "CALL"),
    ):
        append(
            db,
            opening(source=source)
            + [
                event(
                    3, "action_confirmed", action(player="player-1", action=chosen), source=source
                )
            ],
            origin=origin,
        )
        profile = profiles(db, origin, source)["players"][0]
        assert profile["contexts"][0]["counts"][chosen] == 1
        assert profile["action_observations"] == 1
        context = examples(db, origin, source)["examples"][0]["context"]
        assert "hero_cards" not in context and "observed_turn" not in context
    assert profiles(db, ORIGIN + "c", "one")["players"] == []
    assert profiles(db, ORIGIN, "missing")["through_sequence"] == "0"


def test_forced_payments_are_not_voluntary_behavior_observations(db):
    forced = action()
    forced.update(action="PAY_MUSHROOM", amount_delta=20)
    append(db, opening() + [event(3, "action_confirmed", forced)])
    response = profiles(db, ORIGIN, "source-1", player_ids=["hero", "player-3"])
    assert response["through_sequence"] == "3"
    assert all(p["action_observations"] == 0 and p["contexts"] == [] for p in response["players"])
    assert examples(db, ORIGIN, "source-1")["examples"] == []


def test_outcome_is_separate_late_and_never_added_to_old_context(db):
    events = opening() + decision(3) + [event(5, "action_confirmed", action())]
    state = append(db, events)
    before = examples(db, ORIGIN, "source-1")["examples"][0]
    state = append(
        db,
        [
            event(
                6,
                "hand_started",
                {
                    "hand_number": 2,
                    "history_complete": True,
                },
                hand=2,
            )
        ],
        state=state,
    )
    append(db, [settlement(7)], state=state)
    after = examples(db, ORIGIN, "source-1")["examples"][0]
    assert after["context"] == before["context"]
    assert after["hand_outcome"]["eligible_for_completed_hand_dataset"]
    assert after["hand_outcome"]["available_sequence"] == "7"
    assert "board" not in after["hand_outcome"]
    assert examples(db, ORIGIN, "source-1", as_of=6)["examples"][0] == before


@pytest.mark.parametrize("gap_before_action", [True, False])
def test_gap_quarantines_whole_hand_outcomes_without_erasing_observed_actions(
    db, gap_before_action
):
    gap = event(5 if gap_before_action else 6, "capture_gap", {"reason": "synthetic loss"})
    observed = event(6 if gap_before_action else 5, "action_confirmed", action())
    append(
        db,
        opening()
        + decision(3)
        + ([gap, observed] if gap_before_action else [observed, gap])
        + [settlement(7)],
    )
    item = examples(db, ORIGIN, "source-1")["examples"][0]
    assert item["hand_outcome"] is None
    assert item["quality"] == (
        "incomplete_history" if gap_before_action else "complete_observed_history"
    )
    assert profiles(db, ORIGIN, "source-1")["players"][0]["action_observations"] == 1


def test_missing_original_confirmation_does_not_fabricate_a_predecision_context(db):
    append(db, opening() + [event(3, "action_updated", action())])
    item = examples(db, ORIGIN, "source-1")["examples"][0]
    assert item["quality"] == "incomplete_history"
    assert not item["context"]["available"]
    assert item["context"]["hero_cards"] == [None, None]


@pytest.mark.parametrize("observed_street", ["flop", "preflop"])
def test_late_preflop_action_cannot_freeze_future_board_even_with_stale_street(db, observed_street):
    late = copy.deepcopy(opening()[1].model_dump(by_alias=True))
    late["sequence"] = "3"
    late["data"].update(street=observed_street, board=["2c", "3d", "4h"])
    append(
        db, opening() + [BusinessEvent.model_validate(late), event(4, "action_confirmed", action())]
    )
    item = examples(db, ORIGIN, "source-1")["examples"][0]
    assert item["quality"] == "incomplete_history"
    assert item["context"]["observed_street"] == observed_street
    assert not item["context"]["available"]
    assert item["context"]["board"] == []
    assert item["context"]["hero_cards"] == [None, None]
    assert item["context"]["players"] == []


def test_zero_observed_decisions_do_not_certify_a_completed_hand_dataset(db):
    append(db, opening() + [settlement(3)])
    outcome = json.loads(db.execute("SELECT body FROM capture_learning_outcomes").fetchone()[0])
    assert not outcome["eligible_for_completed_hand_dataset"]
    assert examples(db, ORIGIN, "source-1")["examples"] == []


def test_out_of_order_action_never_uses_later_actions_as_input_features(db):
    later = action()
    later["source_event_id"] = "3"
    earlier = action(player="player-1")
    earlier["source_event_id"] = "2"
    append(
        db,
        opening()
        + [
            event(3, "action_confirmed", action()),
            event(4, "action_confirmed", later),
            event(5, "action_confirmed", earlier),
        ],
    )
    item = examples(db, ORIGIN, "source-1")["examples"][0]
    assert not item["context"]["available"]
    assert item["context"]["history_reference"] is None
    assert item["context"]["players"] == []


def test_projection_participates_in_callers_atomic_transaction(db):
    with pytest.raises(RuntimeError), db:
        state = None
        for e in opening() + [event(3, "action_confirmed", action())]:
            after = reduce_event(state, e)
            observe(db, ORIGIN, e, state, after)
            state = after
        raise RuntimeError("receipt write failed")
    assert profiles(db, ORIGIN, "source-1")["through_sequence"] == "0"
    assert examples(db, ORIGIN, "source-1")["examples"] == []


def test_conflicting_settlement_remains_rejected_after_bounded_projection_forgets_it(db):
    state = append(db, opening() + [event(3, "action_confirmed", action()), settlement(4)])
    bad = copy.deepcopy(settlement(5).model_dump(by_alias=True))
    bad["data"]["players"][0]["net_chips"] = 4
    bad["data"]["players"][1]["net_chips"] = -4
    changed = BusinessEvent.model_validate(bad)
    state["settlements"] = []
    with pytest.raises(ValueError, match="Conflicting learned hand outcome"):
        append(db, [changed], state=state)
    assert profiles(db, ORIGIN, "source-1")["through_sequence"] == "4"


@pytest.mark.parametrize("cutoff", [-1, True, 1, "999", "evil", 1.5])
def test_invalid_or_future_cutoffs_rejected(db, cutoff):
    with pytest.raises(ValueError):
        profiles(db, ORIGIN, "source-1", as_of=cutoff)


def test_requested_players_and_response_limits_are_validated(db):
    for players in (["hero", "hero"], [1], ["account-secret"], ["hero"] * 65):
        with pytest.raises(ValueError):
            profiles(db, ORIGIN, "source-1", player_ids=players)
    for limit in (0, True, 101):
        with pytest.raises(ValueError):
            examples(db, ORIGIN, "source-1", limit)


def test_old_hand_scoped_gap_invalidates_the_actual_current_hand(db):
    state = append(
        db,
        opening()
        + decision(3)
        + [
            event(5, "action_confirmed", action()),
            event(6, "hand_started", {"hand_number": 2, "history_complete": True}, hand=2),
        ]
        + decision(7, hand=2)
        + [
            event(9, "action_confirmed", action(), hand=2),
            event(10, "capture_gap", {"reason": "late old-hand gap"}, hand=1),
        ],
    )
    assert state["hand_id"] == "table-1:2" and state["gap"]
    append(
        db,
        [
            event(11, "hand_started", {"hand_number": 3, "history_complete": True}, hand=3),
            settlement(12, hand=2),
        ],
        state=state,
    )
    item = next(
        x for x in examples(db, ORIGIN, "source-1")["examples"] if x["hand_id"] == "table-1:2"
    )
    assert item["hand_outcome"] is None
    assert item["context"]["history_reference"] == {"hand_id": "table-1:2", "through_sequence": "8"}


def test_history_references_grow_linearly_instead_of_copying_every_prefix(db):
    sizes = []
    for count in (32, 64, 128, 256):
        source = f"synthetic-load-{count}"
        events = opening(source=source)
        for i in range(count):
            label = action(player=PLAYERS[i % 4]["player_id"], action="CHECK")
            label["source_event_id"] = str(i + 1)
            events.extend(decision(i * 3 + 3, source=source, player=label["player_id"]))
            events.append(event(i * 3 + 5, "action_confirmed", label, source=source))
        append(db, events)
        size = db.execute(
            "SELECT SUM(length(context)+length(label)) FROM capture_learning_actions "
            "WHERE source=?",
            (source,),
        ).fetchone()[0]
        sizes.append(size)
        exported = examples(db, ORIGIN, source)
        assert exported["origin"] == ORIGIN and exported["identity_scope"] == "origin_source"
        for item in exported["examples"]:
            assert "previous_actions" not in item["context"]
            assert item["context"]["history_reference"] == {
                "hand_id": "table-1:1",
                "through_sequence": str(int(item["observed_sequence"]) - 1),
            }
    # Allow decimal index growth; doubling actions must not quadruple stored context.
    assert all(
        larger < smaller * 2.1 for smaller, larger in zip(sizes[:-1], sizes[1:], strict=True)
    )
