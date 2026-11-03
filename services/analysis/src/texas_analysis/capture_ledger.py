"""Derive observed mushroom rights after a proven whole-pool award.

The caller supplies one source's settlements available at the current event
prefix, under the confirmed rule that a small-blind sole winner receives the
entire pool. This proves neither those source rules nor strategy coverage.
No pre-anchor cash, top-up, private card, or missing opening balance is inferred.
"""

import json
from collections import deque
from collections.abc import Mapping, Sequence

from .capture_contract import SAFE, alias, decimal, table
from .capture_learning import _cutoff


def _hand(value):
    if not isinstance(value, str):
        raise ValueError("A scoped hand identifier is required")
    scope, separator, number = value.partition(":")
    if not separator:
        raise ValueError("A scoped hand identifier is required")
    table(scope)
    decimal(number)
    if int(number) > SAFE:
        raise ValueError("Hand number exceeds the observation contract")
    return scope, int(number)


def _fees(values):
    if not isinstance(values, Mapping) or not 4 <= len(values) <= 8:
        raise ValueError("Confirmed fees must cover four to eight distinct participants")
    for player, amount in values.items():
        if not isinstance(player, str):
            raise ValueError("Expected a scoped player alias")
        alias(player)
        if type(amount) is not int or amount not in (20, 40):
            raise ValueError("Confirmed mushroom fees must be 20 or 40 points")
    if tuple(values.values()).count(40) != 1:
        raise ValueError("Exactly one small-blind fee of 40 points is required")
    return dict(sorted(values.items()))


def _settlement(record):
    if record.get("status") != "reconciled" or record.get("reason") is not None:
        raise ValueError("The settlement chain contains an unreconciled hand")
    players = record.get("players")
    if not isinstance(players, (list, tuple)) or not 4 <= len(players) <= 8:
        raise ValueError("Settlement participants are incomplete")
    paid, awarded, net = {}, {}, 0
    for player in players:
        if not isinstance(player, Mapping):
            raise ValueError("Invalid settlement participant")
        identity = player.get("player_id")
        if not isinstance(identity, str) or identity in paid:
            raise ValueError("Settlement players must have distinct scoped aliases")
        paid[identity] = player.get("mushroom_paid")
        award, change = player.get("mushroom_awarded"), player.get("net_chips")
        if type(award) is not int or not 0 <= award <= SAFE:
            raise ValueError("Settlement awards must be known nonnegative integer points")
        if type(change) is not int or not -SAFE <= change <= SAFE:
            raise ValueError("Settlement net points must be known integers")
        if award:
            awarded[identity] = award
        net += change
    paid = _fees(paid)
    pool_change = sum(paid.values()) - sum(awarded.values())
    if (
        type(record.get("pool_change")) is not int
        or record["pool_change"] != pool_change
        or net + pool_change != 0
    ):
        raise ValueError("Settlement mushroom changes do not conserve points")
    if len(awarded) > 1 or any(paid[player] != 40 for player in awarded):
        raise ValueError("Only the small blind may receive a single whole-pool award")
    winner, amount = next(iter(awarded.items()), (None, 0))
    if amount and amount < sum(paid.values()):
        raise ValueError("Whole-pool award is smaller than the hand's known fees")
    return paid, winner, amount


def derive_mushroom_ledger(*, hand_id, fees, settlements):
    """Return current pool/rights, or reject an unproven observed settlement chain.

    ``fees`` are the current public replay's complete, confirmed payments.
    ``settlements`` must already be bounded to the caller's current source and
    event prefix. Historical hand numbers alone cannot prove reception time.
    The earliest available valid reset anchors the chain. Missing/unreconciled
    hands make rights unknown until another independent whole-pool award clears
    them. An award contradicting a known balance always fails before any recovery.
    """
    scope, current = _hand(hand_id)
    fees = _fees(fees)
    if not isinstance(settlements, Sequence) or isinstance(settlements, (str, bytes)):
        raise ValueError("A bounded settlement sequence is required")
    if len(settlements) > 8:
        raise ValueError("Settlement window exceeds the observation projection")
    by_hand = {}
    for record in settlements:
        if not isinstance(record, Mapping):
            raise ValueError("Invalid settlement record")
        record_scope, number = _hand(record.get("hand_id"))
        if (
            record_scope != scope
            or record.get("table_id") != scope
            or type(record.get("hand_number")) is not int
            or record["hand_number"] != number
        ):
            raise ValueError("Settlement table or hand scope mismatch")
        if number >= current:
            raise ValueError("Current or future hand settlements cannot anchor a live decision")
        if number in by_hand:
            raise ValueError("Duplicate settlement hand")
        by_hand[number] = record
    return _derive_ordered(scope, current, fees, sorted(by_hand.items()))


def derive_recorded_mushroom_ledger(db, *, origin, source, through, hand_id, fees, check=None):
    """Replay immutable outcomes at this source prefix, independently of the UI's eight hands."""
    scope, current = _hand(hand_id)
    fees = _fees(fees)
    cutoff = _cutoff(db, origin, source, through)
    if check is not None:
        check()

    def records():
        previous = 0
        rows = db.execute(
            "SELECT hand,body FROM capture_learning_outcomes "
            "WHERE origin=? AND source=? AND sequence<=? "
            "AND substr(hand,1,length(?)+1)=?||':' ORDER BY length(hand),hand",
            (origin, source, cutoff, scope, scope),
        )
        for hand, body in rows:
            if check is not None:
                check()
            record_scope, number = _hand(hand)
            if record_scope != scope or number <= previous:
                raise ValueError("Stored settlements must have distinct ordered hands")
            if number >= current:
                raise ValueError("Current or future hand settlements cannot anchor a live decision")
            previous = number
            record = json.loads(body)
            if not isinstance(record, Mapping):
                raise ValueError("Invalid stored settlement")
            yield number, {**record, "hand_id": hand}

    return _derive_ordered(scope, current, fees, records())


def _derive_ordered(scope, current, fees, records):
    anchor = expected = outstanding = None
    # Full history remains in SQLite; a long session need not copy every proof into RAM.
    evidence, unknown_reasons = deque(maxlen=17), set()
    for number, record in records:
        parsed = None
        if anchor is None:
            if record.get("status") != "reconciled":
                continue
            parsed = _settlement(record)
            if not parsed[2]:
                continue
            anchor = expected = number
            outstanding = {}
        if number != expected:
            outstanding = None
            unknown_reasons.add("missing_settlement")
            evidence.append(
                {
                    "hand_id": f"{scope}:{expected}",
                    "through_hand_id": f"{scope}:{number - 1}",
                    "kind": "ledger_unknown",
                    "reason": "missing_settlement",
                    "pool_after": None,
                }
            )
        expected = number + 1
        if record.get("status") == "unresolved":
            outstanding = None
            unknown_reasons.add("unreconciled_settlement")
            evidence.append(
                {
                    "hand_id": record["hand_id"],
                    "kind": "ledger_unknown",
                    "reason": "unreconciled_settlement",
                    "pool_after": None,
                }
            )
            continue
        paid, winner, amount = parsed or _settlement(record)
        reanchored = outstanding is None and bool(amount)
        if number != anchor and outstanding is not None:
            for player, contribution in paid.items():
                outstanding[player] = outstanding.get(player, 0) + contribution
            if amount and amount != sum(outstanding.values()):
                raise ValueError("Whole-pool award disagrees with the known pool")
        if amount:
            outstanding = {}
            if reanchored:
                anchor = number
        evidence.append(
            {
                "hand_id": record["hand_id"],
                "kind": "whole_pool_award_reset" if number == anchor else "reconciled_settlement",
                "paid": paid,
                "awarded_to": winner,
                "awarded": amount,
                "pool_after": None if outstanding is None else sum(outstanding.values()),
            }
        )
        if reanchored:
            evidence[-1]["reanchor_reasons"] = sorted(unknown_reasons)
            unknown_reasons.clear()
    if anchor is None:
        raise ValueError("No observed whole-pool award establishes an opening ledger")
    if expected != current or outstanding is None:
        raise ValueError(
            "Settlement history has a gap or unreconciled hand without a later whole-pool award"
        )
    for player, contribution in fees.items():
        outstanding[player] = outstanding.get(player, 0) + contribution
    pool = sum(outstanding.values())
    evidence.append(
        {
            "hand_id": f"{scope}:{current}",
            "kind": "confirmed_current_fees",
            "paid": fees,
            "pool_after": pool,
        }
    )
    return {
        "pool": pool,
        "outstanding": dict(sorted(outstanding.items())),
        "anchor_hand_id": f"{scope}:{anchor}",
        "evidence": list(evidence),
    }
