"""Synthetic observed events; real capture prefixes stay outside the repository."""

from copy import deepcopy

import pytest

from texas_analysis.capture_replay import ReplayUnavailable, replay_capture

PLAYERS = ("hero", "player-1", "player-2", "player-3")
SEATS = (1, 2, 4, 7)


def add_event(events, kind, data, *, hand=None):
    original = events[0]
    events.append(
        {
            **{
                key: original[key]
                for key in (
                    "schema",
                    "type",
                    "adapter_version",
                    "source_id",
                    "captured_at",
                    "table_id",
                    "hand_id",
                )
            },
            "sequence": str(int(events[-1]["sequence"]) + 1),
            "kind": kind,
            "data": deepcopy(data),
        }
    )
    if hand is not None:
        events[-1]["hand_id"] = f"table-1:{hand}"


def opening_events(*, stacks=(800,) * 4, hand=1, source="replay-source"):
    """A complete synthetic pre-fee/forced-bet opening, reusable by store tests."""
    events = [
        {
            "schema": "tgto.capture/0.1",
            "type": "business_event",
            "adapter_version": "wpk/1",
            "source_id": source,
            "sequence": "1",
            "captured_at": "2026-11-02T00:00:00Z",
            "table_id": "table-1",
            "hand_id": f"table-1:{hand}",
            "kind": "hand_started",
            "data": {"hand_number": hand, "history_complete": True},
        }
    ]
    for i, player in enumerate(PLAYERS):
        add_action(events, player, "PAY_MUSHROOM", 40 if i == 1 else 20, round="CLEAN")
    for player, action, amount in zip(
        PLAYERS[1:], ("SMALL_BLIND", "BIG_BLIND", "THIRD_BLIND"), (2, 4, 8), strict=True
    ):
        add_action(events, player, action, amount, total=amount, round="BET_BLIND")
    cash = tuple(
        stack - fee - forced
        for stack, fee, forced in zip(stacks, (20, 40, 20, 20), (0, 2, 4, 8), strict=True)
    )
    add_snapshot(events, cash, actor="hero", call=8)
    return events


def add_action(events, player, action, delta, *, total=None, round="PRE_FLOP"):
    count = sum(event["kind"] == "action_confirmed" for event in events)
    add_event(
        events,
        "action_confirmed",
        {
            "source_event_id": str(count + 1),
            "player_id": player,
            "action": action,
            "amount_delta": delta,
            "amount_to": total,
            "source_round": round,
        },
    )


def add_snapshot(events, cash, *, street="preflop", board=(), actor=None, call=None):
    add_event(
        events,
        "snapshot",
        {
            "street": street,
            "hero_cards": ["As", "Kd"],
            "board": list(board),
            "players": [
                {"player_id": player, "seat": seat, "stack": amount}
                for player, seat, amount in zip(PLAYERS, SEATS, cash, strict=True)
            ],
            "turn": None
            if actor is None
            else {
                "player_id": actor,
                "legal_actions": ["FOLD", "CALL", "RAISE", "ALL_IN"] if actor == "hero" else [],
                "call_amount": call if actor == "hero" else None,
                "min_raise_reported": None,
                "max_raise_reported": None,
            },
            "history_complete": True,
            "rules_version": "wpk-observed-unverified-v1",
        },
    )


def resequence(events):
    for index, event in enumerate(events):
        event["sequence"] = str(index + 1)


def test_opening_uses_only_paid_roster_and_explicit_post_blind_cash_anchor():
    events = opening_events()
    events[-1]["data"]["players"].append({"player_id": "player-4", "seat": 8, "stack": 10})
    result = replay_capture(events)
    assert result.player_ids == PLAYERS and result.seats == SEATS
    assert result.opening_cash == (800,) * 4
    assert result.fees == dict(zip(PLAYERS, (20, 40, 20, 20), strict=True))
    assert result.opening_sequence == result.through_sequence == "9"
    assert result.state.stacks == (780, 758, 776, 772)
    assert result.game.button == 0  # Effective betting button, not physical seat zero.
    assert not hasattr(result.state, "hole_cards")


def test_transitional_clean_snapshot_waits_for_explicit_preflop_before_anchor():
    events = opening_events()
    events[-1]["data"].update(street="between_hands", turn=None)
    with pytest.raises(ReplayUnavailable, match="missing_opening_snapshot"):
        replay_capture(events)
    add_snapshot(events, (780, 758, 776, 772), actor="hero", call=8)
    assert replay_capture(events).opening_sequence == "10"


def test_snapshot_is_the_atomic_boundary_for_stack_update_and_confirmed_action():
    events = opening_events()
    people = deepcopy(events[-1]["data"]["players"])
    people[0]["stack"] -= 8
    add_event(events, "players_updated", {"players": people})
    with pytest.raises(ReplayUnavailable, match="awaiting_atomic_snapshot"):
        replay_capture(events)
    add_action(events, "hero", "CALL", 8, total=8)
    with pytest.raises(ReplayUnavailable, match="awaiting_atomic_snapshot"):
        replay_capture(events)
    add_snapshot(events, (772, 758, 776, 772), actor="player-1")
    result = replay_capture(events)
    assert result.state.stacks == (772, 758, 776, 772)
    assert result.game.actor(result.state) == 1


def test_actual_legal_tree_outside_abstraction_is_replayed_without_claiming_policy_coverage():
    events = opening_events()
    add_action(events, "hero", "RAISE", 34, total=34)
    add_snapshot(events, (746, 758, 776, 772), actor="player-1")
    result = replay_capture(events)
    assert result.state.history[-1].action == "raise_to:34"
    assert not result.state.history[-1].in_action_abstraction


def test_missing_fold_check_totals_and_between_street_snapshot_are_unambiguous():
    events = opening_events()
    for player, action, delta, total in zip(
        PLAYERS, ("CALL", "CALL", "CALL", "CHECK"), (8, 6, 4, 0), (8, 8, 8, None), strict=True
    ):
        add_action(events, player, action, delta, total=total)
    add_snapshot(events, (772, 752, 772, 772), street="preflop")
    result = replay_capture(events)
    assert result.game.actor(result.state) == -1 and result.state.pending_deal == (-1,) * 3
    add_event(events, "board_updated", {"cards": ["2c", "3d", "4h"]})
    add_action(events, "player-1", "CHECK", 0, round="FLOP")
    add_snapshot(
        events, (772, 752, 772, 772), street="flop", board=("2c", "3d", "4h"), actor="player-2"
    )
    assert replay_capture(events).state.history[-1].action == "check"


@pytest.mark.parametrize(
    "cash,delta,action,total", [(26, 6, "call", 6), (800, 780, "raise_to:780", 780)]
)
def test_all_in_is_mapped_to_call_or_raise_using_observed_debit_and_engine_state(
    cash, delta, action, total
):
    events = opening_events(stacks=(cash, 800, 800, 800))
    add_action(events, "hero", "ALL_IN", delta, total=total)
    add_snapshot(events, (0, 758, 776, 772), actor="player-1")
    result = replay_capture(events)
    assert result.state.history[-1].action == action
    assert result.state.stacks[0] == 0


def returned_events():
    events = opening_events(stacks=(120,) * 4)
    add_action(events, "hero", "ALL_IN", 100, total=100)
    add_action(events, "player-1", "ALL_IN", 78, total=80)
    add_action(events, "player-2", "FOLD", 0)
    add_action(events, "player-3", "FOLD", 0)
    add_snapshot(events, (0, 0, 96, 92))
    return events


def test_only_exact_uncalled_return_can_be_uncredited_during_uncontested_runout():
    events = returned_events()
    result = replay_capture(events)
    assert result.state.returned == result.pending_returns == (20, 0, 0, 0)
    assert result.game.actor(result.state) == -1 and result.state.stacks[0] == 20
    events[-1]["data"]["players"][0]["stack"] = 1
    with pytest.raises(ReplayUnavailable, match="snapshot_cash_conflict"):
        replay_capture(events)


def test_wagering_and_payout_cash_never_become_the_same_public_state():
    events = opening_events()
    for player in PLAYERS[:3]:
        add_action(events, player, "FOLD", 0)
    add_snapshot(events, (780, 758, 776, 786))
    result = replay_capture(events)
    assert result.state.awaiting_settlement and result.game.actor(result.state) is None
    assert result.pending_returns is None
    assert result.state.stacks != (780, 758, 776, 786)  # No invented payout.
    events[-1]["data"]["turn"] = deepcopy(opening_events()[-1]["data"]["turn"])
    with pytest.raises(ReplayUnavailable, match="terminal_turn_conflict"):
        replay_capture(events)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda e: e[0]["data"].update(history_complete=False), "incomplete_observed_history"),
        (lambda e: e[1]["data"].update(amount_delta=21), "unsupported_mushroom_fee"),
        (lambda e: e[5]["data"].update(action="ANTE", amount_delta=4), "ante_not_supported"),
        (lambda e: e[7]["data"].update(amount_delta=16), "unsupported_forced_bets"),
        (lambda e: e[-1]["data"]["players"][0].update(stack=None), "missing_opening_stacks"),
        (
            lambda e: e[-1]["data"].update(street="flop", board=["2c", "3d", "4h"]),
            "late_opening_snapshot",
        ),
        (lambda e: e[6]["data"].update(player_id="player-3"), "conflicting_blind_players"),
        (lambda e: e[2]["data"].update(amount_delta=20), "fee_owner_conflicts_with_small_blind"),
    ],
)
def test_incomplete_or_unsupported_openings_are_rejected(mutation, reason):
    events = opening_events()
    mutation(events)
    with pytest.raises(ReplayUnavailable, match=reason):
        replay_capture(events)


@pytest.mark.parametrize(
    "kind,delta,round,reason",
    [
        ("CALL", 7, "PRE_FLOP", "illegal_observed_betting_history"),
        ("CALL", 8, None, "unknown_or_conflicting_action_street"),
        ("CALL", 8, "FLOP", "unknown_or_conflicting_action_street"),
        ("ANTE", 1, "PRE_FLOP", "unsupported_action_or_forced_bet"),
        ("ALL_IN", 8, "PRE_FLOP", "all_in_cash_conflict"),
    ],
)
def test_unknown_street_ante_and_invalid_action_amounts_cannot_repair_cash(
    kind, delta, round, reason
):
    events = opening_events()
    add_action(events, "hero", kind, delta, round=round)
    add_snapshot(events, (772, 758, 776, 772))
    with pytest.raises(ReplayUnavailable, match=reason):
        replay_capture(events)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda d: d["players"][0].update(stack=800), "snapshot_cash_conflict"),
        (lambda d: d["players"][0].update(seat=3), "participant_changed_seat"),
        (lambda d: d["players"].pop(), "participant_disappeared"),
        (lambda d: d.update(street="unknown", turn=None), "snapshot_street_conflict"),
        (
            lambda d: d["turn"].update(player_id="player-1", legal_actions=[], call_amount=None),
            "snapshot_actor_conflict",
        ),
        (lambda d: d["turn"].update(call_amount=9), "snapshot_call_amount_conflict"),
    ],
)
def test_later_atomic_snapshots_cannot_hide_topups_seat_changes_or_conflicts(mutation, reason):
    events = opening_events()
    add_snapshot(events, (780, 758, 776, 772), actor="hero", call=8)
    mutation(events[-1]["data"])
    with pytest.raises(ReplayUnavailable, match=reason):
        replay_capture(events)


def test_missing_initial_snapshot_cannot_be_reconstructed_from_later_cash():
    events = opening_events()[:-1]
    add_action(events, "hero", "CALL", 8, total=8)
    add_snapshot(events, (772, 758, 776, 772), actor="player-1")
    with pytest.raises(ReplayUnavailable, match="missing_opening_snapshot"):
        replay_capture(events)


def test_prefix_ends_at_atomic_current_snapshot_but_may_include_previous_hand_settlement():
    events = opening_events(hand=2)
    add_event(
        events,
        "settlement_pending",
        {
            "received": "result",
            "players": [
                {
                    "player_id": "hero",
                    "net_chips": None,
                    "mushroom_paid": None,
                    "mushroom_awarded": None,
                }
            ],
        },
        hand=1,
    )
    assert replay_capture(events).through_sequence == "10"
    events[-1]["hand_id"] = "table-1:2"
    with pytest.raises(ReplayUnavailable, match="awaiting_atomic_snapshot"):
        replay_capture(events)


def test_old_gap_does_not_taint_complete_new_hand_but_new_gap_blocks_replay():
    events = opening_events()
    gap = {**events[0], "kind": "capture_gap", "data": {"reason": "attached_mid_hand"}}
    events.insert(0, gap)
    resequence(events)
    assert replay_capture(events).opening_cash == (800,) * 4
    add_event(events, "capture_gap", {"reason": "business_context_replaced"})
    with pytest.raises(ReplayUnavailable, match="capture_gap_after_opening"):
        replay_capture(events)


def test_sequence_gap_and_mixed_sources_are_never_sorted_away():
    events = opening_events()
    events[1]["source_id"] = "different-source"
    with pytest.raises(ReplayUnavailable, match="mixed_capture_sources"):
        replay_capture(events)
    events[1]["source_id"] = events[0]["source_id"]
    del events[1]
    with pytest.raises(ReplayUnavailable, match="capture_sequence_gap"):
        replay_capture(events)


def late_board_events():
    events = opening_events()
    add_action(events, "hero", "CALL", 8, total=8)
    add_action(events, "player-1", "RAISE", 40, total=42)
    add_action(events, "player-2", "CALL", 38, total=42)
    add_snapshot(events, (772, 718, 738, 772), actor="player-3")
    add_event(events, "board_updated", {"cards": ["2c", "3d", "4h"]})
    add_snapshot(
        events, (772, 718, 738, 738), street="flop", board=("2c", "3d", "4h"), actor="player-1"
    )
    return events


def test_late_prior_street_actions_repair_only_prefixes_that_already_contain_them():
    events = late_board_events()
    missing_prefix = deepcopy(events)
    with pytest.raises(ReplayUnavailable, match="awaiting_prior_street_actions"):
        replay_capture(events)
    add_action(events, "player-3", "CALL", 34)
    add_action(events, "hero", "FOLD", 0)
    add_action(events, "player-1", "BET", 160, total=160, round="FLOP")
    add_snapshot(
        events, (772, 558, 738, 738), street="flop", board=("2c", "3d", "4h"), actor="player-2"
    )
    result = replay_capture(events)
    assert result.state.stacks == (772, 558, 738, 738)
    assert result.state.folded == (True, False, False, False)
    assert result.game.actor(result.state) == 2
    assert [h.action for h in result.state.history[-3:]] == ["call", "fold", "raise_to:160"]
    # A later repair cannot change what was knowable at the earlier decision head.
    with pytest.raises(ReplayUnavailable, match="awaiting_prior_street_actions"):
        replay_capture(missing_prefix)


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda e: e[-3]["data"].update(amount_delta=33), "illegal_observed_betting_history"),
        (
            lambda e: e[-3]["data"].update(source_round="FLOP"),
            "awaiting_prior_street_actions",
        ),
        (
            lambda e: e[-2]["data"].update(source_event_id="15"),
            "observed_action_sequence_gap",
        ),
        (lambda e: e[-4]["data"]["players"][3].update(stack=739), "snapshot_cash_conflict"),
    ],
)
def test_late_history_never_repairs_wrong_amount_street_sequence_or_old_snapshot(mutation, reason):
    events = late_board_events()
    add_action(events, "player-3", "CALL", 34)
    add_action(events, "hero", "FOLD", 0)
    add_snapshot(
        events, (772, 718, 738, 738), street="flop", board=("2c", "3d", "4h"), actor="player-1"
    )
    mutation(events)
    with pytest.raises(ReplayUnavailable, match=reason):
        replay_capture(events)
