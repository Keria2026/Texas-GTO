"""Long sessions use only immutable settlements visible at the requested source prefix."""

import json
import sqlite3
from contextlib import closing

import pytest
from test_capture_ledger import fees, settlement

from texas_analysis.capture_learning import initialize
from texas_analysis.capture_ledger import derive_recorded_mushroom_ledger
from texas_strategy.evaluation import CancellationError


@pytest.fixture
def database():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize(db)
        yield db


def record(db, value, *, sequence=None, origin="origin", source="source"):
    sequence = str(sequence or value["hand_number"]).zfill(20)
    body = {k: value[k] for k in ("players", "pool_change", "status", "reason")}
    db.execute(
        "INSERT INTO capture_learning_outcomes VALUES(?,?,?,?,?)",
        (origin, source, value["hand_id"], sequence, json.dumps(body)),
    )
    db.execute(
        "INSERT INTO capture_learning_heads VALUES(?,?,?) ON CONFLICT(origin,source) "
        "DO UPDATE SET sequence=MAX(sequence,excluded.sequence)",
        (origin, source, sequence),
    )


def derive(db, hand, through, *, check=None):
    return derive_recorded_mushroom_ledger(
        db,
        origin="origin",
        source="source",
        through=str(through),
        hand_id=f"table-1:{hand}",
        fees=fees(),
        check=check,
    )


def test_thousands_of_hands_preserve_old_anchor_and_ownership_with_bounded_evidence(database):
    record(database, settlement(1, award=100))
    for hand in range(2, 5001):
        record(database, settlement(hand))
    result = derive(database, 5001, 5000)
    assert result["pool"] == 500_000
    assert result["outstanding"] == {player: amount * 5000 for player, amount in fees().items()}
    assert result["anchor_hand_id"] == "table-1:1"
    assert len(result["evidence"]) == 17
    assert result["evidence"][-1]["hand_id"] == "table-1:5001"


def test_delayed_settlement_only_repairs_decisions_after_its_arrival(database):
    record(database, settlement(1, award=100), sequence=1)
    record(database, settlement(3), sequence=2)
    record(database, settlement(2), sequence=3)
    with pytest.raises(ValueError, match="gap"):
        derive(database, 4, 2)
    assert derive(database, 4, 3)["pool"] == 300


@pytest.mark.parametrize("different", ["origin", "source", "table"])
def test_other_scopes_cannot_supply_an_anchor(database, different):
    anchor = settlement(1, award=100)
    options = {}
    if different == "table":
        anchor.update(hand_id="table-2:1", table_id="table-2")
    else:
        options[different] = "other"
    record(database, anchor, **options)
    record(database, settlement(2))
    with pytest.raises(ValueError, match="No observed whole-pool award"):
        derive(database, 3, 2)


def test_later_award_does_not_hide_a_known_conflict_outside_the_ui_window(database):
    record(database, settlement(1, award=100))
    record(database, settlement(2, award=200))
    for hand in range(3, 15):
        record(database, settlement(hand, award=1200 if hand == 14 else 0))
    with pytest.raises(ValueError, match="known pool"):
        derive(database, 15, 14)


@pytest.mark.parametrize("kind", ["missing", "unresolved"])
def test_old_gap_still_needs_a_later_independent_award(database, kind):
    record(database, settlement(1, award=100))
    for hand in range(2, 13):
        if hand == 3:
            if kind == "missing":
                continue
            value = settlement(hand)
            value.update(status="unresolved", reason="missing_amounts", pool_change=None)
        else:
            value = settlement(hand)
        record(database, value)
    with pytest.raises(ValueError, match="gap|unreconciled"):
        derive(database, 13, 12)
    record(database, settlement(13, award=1200))
    assert derive(database, 14, 13)["outstanding"] == fees()


@pytest.mark.parametrize("hand", [2, 3])
def test_current_and_future_settlements_cannot_enter_the_ledger(database, hand):
    record(database, settlement(1, award=100))
    record(database, settlement(hand), sequence=3)
    with pytest.raises(ValueError, match="Current or future"):
        derive(database, 2, 3)


def test_long_history_can_be_cancelled_before_becoming_a_ledger(database):
    for hand in range(1, 30):
        record(database, settlement(hand, award=100 if hand == 1 else 0))
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 5:
            raise CancellationError("cancelled")

    with pytest.raises(CancellationError):
        derive(database, 30, 29, check=check)
    assert calls == 5
    assert derive(database, 30, 29)["pool"] == 2900


def test_unavailable_source_prefix_is_rejected(database):
    record(database, settlement(1, award=100))
    with pytest.raises(ValueError, match="available"):
        derive(database, 2, 2)
