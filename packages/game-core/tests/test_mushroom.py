from dataclasses import replace

import pytest

from texas_game_core.mushroom import (
    MushroomLedger,
    MushroomRules,
    MushroomState,
    UnsupportedRuleError,
    apply_mushroom_event,
)

PLAYERS = ("a", "b", "c", "d")


def rules(**overrides):
    values = {
        "base_points": 20,
        "version": "simulation-mushroom-v1",
        "contribution_timing": "before_hand",
        "roster_policy": "explicit_dealt_players",
        "entry_policy": "between_hands",
        "insufficient_policy": "reject",
        "freeze_policy": "no_transfers",
        "remainder_policy": "participant_order",
    }
    values.update(overrides)
    return MushroomRules(**values)


def begin(ledger, hand_id, players=PLAYERS, small_blind="a", chips=None):
    return ledger.begin_hand(
        "begin-" + hand_id,
        hand_id,
        players,
        small_blind,
        dict.fromkeys(players, 1_000) if chips is None else chips,
        timing="before_hand",
    )


def settle(ledger, hand_id, winner, outcome="sole_main_pot"):
    return ledger.settle_hand("settle-" + hand_id, hand_id, winner, outcome=outcome)


def test_all_pay_base_small_blind_pays_double_and_non_blind_carries():
    ledger = MushroomLedger(rules())
    collected = begin(ledger, "h1")
    assert dict(collected.transfers) == {"a": -40, "b": -20, "c": -20, "d": -20}
    assert collected.state.pool == 100
    carried = settle(ledger, "h1", "b")
    assert carried.transfers == ()
    assert carried.reveal_required == ()
    assert carried.state.pool == 100
    assert carried.state.check_conservation()


def test_blind_wins_entire_cross_hand_pool_and_must_reveal_without_cards_in_output():
    ledger = MushroomLedger(rules())
    begin(ledger, "h1")
    settle(ledger, "h1", "d")
    begin(ledger, "h2", small_blind="b")
    payout = settle(ledger, "h2", "b")
    assert payout.transfers == (("b", 200),)
    assert payout.reveal_required == ("b",)
    assert payout.state.pool == 0
    assert dict(payout.state.player_net) == {"a": -60, "b": 140, "c": -40, "d": -40}
    assert payout.state.check_conservation()


def test_three_players_freeze_and_four_resume_without_erasing_escrow():
    ledger = MushroomLedger(rules())
    begin(ledger, "h1")
    settle(ledger, "h1", "b")
    frozen = begin(ledger, "h2", players=("a", "b", "c"))
    assert frozen.state.frozen
    assert frozen.state.pool == 100
    assert frozen.transfers == ()
    frozen_win = settle(ledger, "h2", "a")
    assert frozen_win.transfers == ()
    assert frozen_win.reveal_required == ()
    resumed = begin(ledger, "h3")
    assert not resumed.state.frozen
    assert resumed.state.pool == 200
    assert settle(ledger, "h3", "a").transfers == (("a", 200),)
    assert ledger.snapshot().check_conservation()


def test_end_uses_final_hand_participants_even_after_freeze_with_declared_remainder():
    ledger = MushroomLedger(rules())
    begin(ledger, "h1")
    settle(ledger, "h1", "b")
    begin(ledger, "h2", players=("c", "e", "b"), small_blind="e")
    settle(ledger, "h2", "c")
    ended = ledger.end_session("end")
    assert dict(ended.transfers) == {"c": 34, "e": 33, "b": 33}
    assert ended.state.pool == 0
    assert ended.state.ended
    assert ended.state.check_conservation()
    assert dict(ended.state.player_net)["a"] == -40
    assert dict(ended.state.player_net)["d"] == -20


def test_idempotency_conflict_and_rebuild_conserve_exactly_once():
    ledger = MushroomLedger(rules())
    first = begin(ledger, "h1")
    assert begin(ledger, "h1").duplicate
    assert begin(ledger, "h1").transfers == ()
    assert ledger.snapshot().version == first.state.version
    settle(ledger, "h1", "a")
    assert settle(ledger, "h1", "a").reveal_required == ()
    ledger.end_session("end")
    # Retries after session closure are still valid and cannot pay twice.
    assert begin(ledger, "h1").duplicate
    before = ledger.snapshot()
    with pytest.raises(ValueError, match="Conflicting payload"):
        ledger.record(replace(ledger.events[0], small_blind="b"))
    assert ledger.snapshot() == before
    assert ledger.rebuild((*ledger.events, *ledger.events)) == before
    fresh = MushroomLedger(rules())
    for event in ledger.events:
        transition = apply_mushroom_event(fresh.snapshot(), event, fresh.rules)
        assert fresh.record(event) == transition
    assert fresh.snapshot() == before


def test_duplicate_hand_under_new_event_id_and_out_of_order_events_rejected():
    ledger = MushroomLedger(rules())
    begin(ledger, "h1")
    with pytest.raises(ValueError, match="current hand"):
        begin(ledger, "h2")
    with pytest.raises(ValueError, match="open hand"):
        settle(ledger, "h2", "a")
    settle(ledger, "h1", "b")
    with pytest.raises(ValueError, match="already been recorded"):
        ledger.record(replace(ledger.events[0], event_id="different-event-id"))


@pytest.mark.parametrize(
    "policy",
    [
        "contribution_timing",
        "roster_policy",
        "entry_policy",
        "insufficient_policy",
        "freeze_policy",
    ],
)
def test_unverified_rules_require_explicit_simulation_choices(policy):
    ledger = MushroomLedger(rules(**{policy: None}))
    with pytest.raises(UnsupportedRuleError, match=policy):
        begin(ledger, "h1")
    assert ledger.snapshot().version == 0
    assert not ledger.events


def test_wrong_timing_missing_blind_insufficient_chips_and_split_pots_are_atomic():
    ledger = MushroomLedger(rules())
    with pytest.raises(UnsupportedRuleError, match="timing"):
        ledger.begin_hand("x", "h1", PLAYERS, "a", dict.fromkeys(PLAYERS, 100), timing="after")
    with pytest.raises(UnsupportedRuleError, match="small blind"):
        begin(ledger, "h1", small_blind="outsider")
    with pytest.raises(UnsupportedRuleError, match="Insufficient"):
        begin(ledger, "h1", chips={"a": 39, "b": 20, "c": 20, "d": 20})
    assert ledger.snapshot().version == 0
    begin(ledger, "h1")
    before = ledger.snapshot()
    for outcome in ("split_pot", "side_pot", "unknown"):
        with pytest.raises(UnsupportedRuleError, match="Split pots"):
            settle(ledger, "h1", "a", outcome)
    assert ledger.snapshot() == before
    with pytest.raises(ValueError, match="Settle the final hand"):
        ledger.end_session("end")


def test_terminal_remainder_rejection_is_explicit_and_atomic():
    ledger = MushroomLedger(rules(remainder_policy="reject"))
    begin(ledger, "h1")
    settle(ledger, "h1", "b")
    begin(ledger, "h2", players=("a", "b", "c"))
    settle(ledger, "h2", "a")
    before = ledger.snapshot()
    with pytest.raises(UnsupportedRuleError, match="divided exactly"):
        ledger.end_session("end")
    assert ledger.snapshot() == before
    unresolved = MushroomLedger(rules(remainder_policy=None))
    with pytest.raises(UnsupportedRuleError, match="remainder_policy"):
        unresolved.end_session("end")


@pytest.mark.parametrize("value", [True, 1.5, -1])
def test_integer_chips_only(value):
    with pytest.raises(ValueError, match="integer"):
        rules(base_points=value)
    ledger = MushroomLedger(rules())
    with pytest.raises(ValueError, match="integer"):
        begin(ledger, "h1", chips=dict.fromkeys(PLAYERS, value))


def test_rebuild_failure_does_not_replace_valid_log():
    ledger = MushroomLedger(rules())
    begin(ledger, "h1")
    before, events = ledger.snapshot(), ledger.events
    with pytest.raises(ValueError, match="Conflicting payload"):
        ledger.rebuild((*events, replace(events[0], small_blind="b")))
    assert ledger.snapshot() == before
    assert ledger.events == events


def test_external_state_cannot_hide_fractional_or_duplicated_money_entries():
    assert not MushroomState("v", pool=1.5, player_net=(("a", -1.5),)).check_conservation()
    assert not MushroomState("v", pool=True, player_net=(("a", -1),)).check_conservation()
    assert not MushroomState("v", pool=2, player_net=(("a", -1), ("a", -1))).check_conservation()


def test_session_refunds_unawarded_actual_fees_to_every_original_contributor():
    ledger = MushroomLedger(rules(settlement_policy="refund_contributions"))
    begin(ledger, "h1", small_blind="a")
    settle(ledger, "h1", "b")
    begin(ledger, "h2", players=("b", "c", "d", "e"), small_blind="b")
    settle(ledger, "h2", "c")
    assert dict(ledger.snapshot().outstanding_contributions) == {
        "a": 40,
        "b": 60,
        "c": 40,
        "d": 40,
        "e": 20,
    }
    ended = ledger.end_session("end")
    assert dict(ended.transfers) == {"a": 40, "b": 60, "c": 40, "d": 40, "e": 20}
    assert ended.state.pool == 0
    assert all(amount == 0 for _player, amount in ended.state.player_net)
    assert ledger.end_session("end").transfers == ()
    assert ledger.rebuild(ledger.events) == ended.state


def test_won_pool_fees_are_not_refunded_a_second_time_at_session_end():
    ledger = MushroomLedger(rules(settlement_policy="refund_contributions"))
    begin(ledger, "h1", small_blind="a")
    settle(ledger, "h1", "a")
    assert ledger.snapshot().outstanding_contributions == ()
    begin(ledger, "h2", small_blind="b")
    settle(ledger, "h2", "c")
    ended = ledger.end_session("end")
    assert dict(ended.transfers) == {"a": 20, "b": 40, "c": 20, "d": 20}
    assert dict(ended.state.player_net) == {"a": 60, "b": -20, "c": -20, "d": -20}
    assert ended.state.check_conservation()
