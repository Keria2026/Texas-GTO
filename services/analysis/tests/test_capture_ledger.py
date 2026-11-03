"""Observed mushroom rights require a complete past-only settlement chain."""

from copy import deepcopy

import pytest

from texas_analysis.capture_ledger import derive_mushroom_ledger


def fees(small_blind="hero", *, participants=("hero", "player-1", "player-2", "player-3")):
    return {player: 40 if player == small_blind else 20 for player in participants}


def settlement(hand, *, paid=None, award=0):
    paid = fees() if paid is None else paid
    winner = next(player for player, amount in paid.items() if amount == 40)
    return {
        "hand_id": f"table-1:{hand}",
        "table_id": "table-1",
        "hand_number": hand,
        "status": "reconciled",
        "reason": None,
        "pool_change": sum(paid.values()) - award,
        "players": [
            {
                "player_id": player,
                "mushroom_paid": amount,
                "mushroom_awarded": award if player == winner else 0,
                "net_chips": -amount + (award if player == winner else 0),
            }
            for player, amount in paid.items()
        ],
    }


def derive(hand, records, *, paid=None):
    return derive_mushroom_ledger(
        hand_id=f"table-1:{hand}", fees=fees() if paid is None else paid, settlements=records
    )


def test_anchor_then_accumulate_known_fees_without_mutating_or_exposing_cards():
    records = [settlement(2, paid=fees("player-1")), settlement(1, award=100)]
    records[0].update(hero_cards=["As", "Kd"], board=["2h", "3h", "4h"])
    original = deepcopy(records)
    result = derive(3, records)
    assert result["pool"] == 200
    assert result["outstanding"] == {"hero": 60, "player-1": 60, "player-2": 40, "player-3": 40}
    assert result["anchor_hand_id"] == "table-1:1"
    assert [e["pool_after"] for e in result["evidence"]] == [0, 100, 200]
    assert [e["kind"] for e in result["evidence"]] == [
        "whole_pool_award_reset",
        "reconciled_settlement",
        "confirmed_current_fees",
    ]
    assert all("hero_cards" not in e and "board" not in e for e in result["evidence"])
    result["evidence"][1]["paid"]["hero"] = 999
    assert records == original


def test_anchor_clears_unknown_earlier_pool_without_inventing_its_owners():
    result = derive(2, [settlement(1, award=250)])
    assert result["pool"] == 100
    assert result["outstanding"] == fees()
    assert result["evidence"][0]["awarded"] == 250
    assert result["evidence"][0]["pool_after"] == 0


def test_later_whole_pool_award_clears_accumulated_rights():
    result = derive(4, [settlement(1, award=100), settlement(2), settlement(3, award=200)])
    assert result["pool"] == 100
    assert result["outstanding"] == fees()
    assert result["anchor_hand_id"] == "table-1:1"
    assert [e["pool_after"] for e in result["evidence"]] == [0, 100, 0, 100]


@pytest.mark.parametrize("award", [100, 150, 250])
def test_newer_reset_cannot_hide_a_conflicting_known_award(award):
    with pytest.raises(ValueError, match="known pool"):
        derive(4, [settlement(1, award=100), settlement(2), settlement(3, award=award)])


def test_departed_players_keep_their_outstanding_rights():
    current = fees(participants=("hero", "player-1", "player-2", "player-4"))
    result = derive(3, [settlement(1, award=100), settlement(2)], paid=current)
    assert result["outstanding"] == {
        "hero": 80,
        "player-1": 40,
        "player-2": 40,
        "player-3": 20,
        "player-4": 20,
    }
    assert result["pool"] == 200


@pytest.mark.parametrize("records", [[], [settlement(1)], [settlement(n) for n in range(2, 10)]])
def test_no_anchor_never_becomes_a_zero_opening_balance(records):
    with pytest.raises(ValueError, match="No observed whole-pool award"):
        derive(10, records)


def test_missing_hand_after_earliest_anchor_without_newer_award_is_rejected():
    with pytest.raises(ValueError, match="gap"):
        derive(4, [settlement(1, award=100), settlement(3)])


def test_unreconciled_hand_after_anchor_without_newer_award_is_rejected():
    unknown = settlement(2)
    unknown.update(status="unresolved", reason="missing_amounts", pool_change=None)
    with pytest.raises(ValueError, match="unreconciled"):
        derive(4, [settlement(1, award=100), unknown, settlement(3)])


def test_missing_hands_become_unknown_until_independent_whole_pool_award():
    result = derive(
        6, [settlement(1, award=100), settlement(2), settlement(4), settlement(5, award=450)]
    )
    assert result["pool"] == 100
    assert result["outstanding"] == fees()
    assert result["anchor_hand_id"] == "table-1:5"
    assert [e["pool_after"] for e in result["evidence"]] == [0, 100, None, None, 0, 100]
    assert result["evidence"][2] == {
        "hand_id": "table-1:3",
        "through_hand_id": "table-1:3",
        "kind": "ledger_unknown",
        "reason": "missing_settlement",
        "pool_after": None,
    }
    assert result["evidence"][4]["kind"] == "whole_pool_award_reset"
    assert result["evidence"][4]["reanchor_reasons"] == ["missing_settlement"]


def test_unreconciled_hand_can_be_cleared_by_later_valid_award():
    unknown = settlement(2)
    unknown.update(status="unresolved", reason="point_difference", pool_change=None)
    result = derive(4, [settlement(1, award=100), unknown, settlement(3, award=300)])
    assert result["outstanding"] == fees()
    assert result["anchor_hand_id"] == "table-1:3"
    assert result["evidence"][1]["reason"] == "unreconciled_settlement"
    assert result["evidence"][1]["pool_after"] is None
    assert result["evidence"][2]["reanchor_reasons"] == ["unreconciled_settlement"]


def test_known_award_conflict_cannot_be_hidden_by_gap_and_later_reset():
    with pytest.raises(ValueError, match="known pool"):
        derive(5, [settlement(1, award=100), settlement(2, award=200), settlement(4, award=300)])


def test_award_after_recovery_must_match_new_known_balance():
    with pytest.raises(ValueError, match="known pool"):
        derive(5, [settlement(1, award=100), settlement(3, award=300), settlement(4, award=200)])


def test_gap_after_successful_recovery_still_requires_another_award():
    with pytest.raises(ValueError, match="gap"):
        derive(5, [settlement(1, award=100), settlement(3, award=300)])


def test_gap_and_unreconciled_hand_both_recorded_before_recovery():
    unknown = settlement(3)
    unknown.update(status="unresolved", reason="missing_amounts", pool_change=None)
    result = derive(5, [settlement(1, award=100), unknown, settlement(4, award=300)])
    assert result["evidence"][-2]["reanchor_reasons"] == [
        "missing_settlement",
        "unreconciled_settlement",
    ]


def test_recovery_preserves_owners_after_new_anchor_without_reusing_unknown_rights():
    previous = fees(participants=("hero", "player-1", "player-2", "player-4"))
    result = derive(
        6,
        [
            settlement(1, award=100),
            settlement(2),
            settlement(4, award=100),
            settlement(5, paid=previous),
        ],
    )
    assert result["outstanding"] == {
        "hero": 80,
        "player-1": 40,
        "player-2": 40,
        "player-3": 20,
        "player-4": 20,
    }
    assert result["pool"] == 200


def test_large_gap_is_recorded_as_one_unknown_interval():
    later = 10**12
    result = derive(later + 1, [settlement(1, award=100), settlement(later, award=100)])
    assert result["pool"] == 100
    assert result["evidence"][1]["through_hand_id"] == f"table-1:{later - 1}"
    assert len(result["evidence"]) == 4


def test_unreconciled_older_hand_can_precede_a_proven_clearing_anchor():
    unknown = settlement(1)
    unknown.update(status="unresolved", reason="missing_amounts", pool_change=None)
    result = derive(3, [unknown, settlement(2, award=250)])
    assert result["pool"] == 100
    assert result["anchor_hand_id"] == "table-1:2"


@pytest.mark.parametrize("number", [2, 3])
def test_current_or_future_settlement_cannot_support_a_live_decision(number):
    with pytest.raises(ValueError, match="Current or future"):
        derive(2, [settlement(1, award=100), settlement(number, award=100)])


@pytest.mark.parametrize(
    "updates",
    [
        {"hand_id": "table-2:1", "table_id": "table-2"},
        {"table_id": "table-2"},
        {"hand_number": 2},
        {"hand_number": True},
    ],
)
def test_mismatched_scope_is_rejected(updates):
    record = settlement(1, award=100)
    record.update(updates)
    with pytest.raises(ValueError, match="scope mismatch"):
        derive(2, [record])


def test_duplicate_hand_is_rejected():
    record = settlement(1, award=100)
    with pytest.raises(ValueError, match="Duplicate"):
        derive(2, [record, deepcopy(record)])


@pytest.mark.parametrize(
    "bad_hand", [None, 2, "table-1:0", "table-1:01", "table-0:1", "table-1:2:3"]
)
def test_hand_id_must_be_canonical(bad_hand):
    with pytest.raises(ValueError):
        derive_mushroom_ledger(hand_id=bad_hand, fees=fees(), settlements=[])


@pytest.mark.parametrize("amount", [None, True, 40.0, -20, 0, 21])
def test_current_fees_must_be_known_integer_rule_amounts(amount):
    paid = fees()
    paid["hero"] = amount
    with pytest.raises(ValueError, match="fees"):
        derive(2, [settlement(1, award=100)], paid=paid)


@pytest.mark.parametrize(
    "paid",
    [
        {"hero": 40, "player-1": 20, "player-2": 20},
        {"hero": 40, **{f"player-{n}": 20 for n in range(1, 9)}},
        {"hero": 20, "player-1": 20, "player-2": 20, "player-3": 20},
        {"hero": 40, "player-1": 40, "player-2": 20, "player-3": 20},
        {"hero": 40, "raw-user-id": 20, "player-2": 20, "player-3": 20},
    ],
)
def test_current_fees_must_cover_a_valid_four_to_eight_player_roster(paid):
    with pytest.raises(ValueError):
        derive(2, [settlement(1, award=100)], paid=paid)


@pytest.mark.parametrize("kind", ["shared", "ordinary_player", "too_small"])
def test_only_a_single_full_pool_award_to_small_blind_can_anchor(kind):
    record = settlement(1, award=50 if kind == "too_small" else 100)
    if kind in ("shared", "ordinary_player"):
        amount = 50 if kind == "shared" else 100
        record["players"][0]["mushroom_awarded"] -= amount
        record["players"][0]["net_chips"] -= amount
        record["players"][1]["mushroom_awarded"] += amount
        record["players"][1]["net_chips"] += amount
    with pytest.raises(ValueError, match="small blind|smaller"):
        derive(2, [record])


@pytest.mark.parametrize(
    ("field", "value"),
    [("net_chips", None), ("net_chips", False), ("mushroom_awarded", None), ("mushroom_paid", 0)],
)
def test_incomplete_settlement_cannot_supply_an_anchor(field, value):
    record = settlement(1, award=100)
    record["players"][0][field] = value
    with pytest.raises(ValueError):
        derive(2, [record])


@pytest.mark.parametrize("kind", ["pool_change", "net_points", "reason", "duplicate_player"])
def test_settlement_must_be_reconciled_and_conserve_points(kind):
    record = settlement(1, award=100)
    if kind == "pool_change":
        record["pool_change"] = 1
    elif kind == "net_points":
        record["players"][0]["net_chips"] += 1
    elif kind == "reason":
        record["reason"] = "point_difference"
    else:
        record["players"][1]["player_id"] = "hero"
    with pytest.raises(ValueError):
        derive(2, [record])


def test_settlement_window_is_bounded_to_projection_size():
    with pytest.raises(ValueError, match="window"):
        derive(10, [settlement(n, award=100) for n in range(1, 10)])
