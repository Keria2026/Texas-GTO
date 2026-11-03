"""Replay observed betting from an explicit opening, never from invented private cards.

The effective button fixes betting order within the paid roster only. This does
not establish the physical table button, mushroom pool ownership or policy coverage.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from texas_game_core.public_holdem import HoldemRules, PublicHoldemGame, PublicHoldemState

from .capture_contract import BusinessEvent

ROUNDS = {"PRE_FLOP": "preflop", "FLOP": "flop", "TURN": "turn", "RIVER": "river"}
FORCED = {"SMALL_BLIND": 2, "BIG_BLIND": 4, "THIRD_BLIND": 8}


class ReplayUnavailable(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PublicReplay:
    game: PublicHoldemGame
    state: PublicHoldemState
    player_ids: tuple[str, ...]
    seats: tuple[int, ...]
    opening_cash: tuple[int, ...]
    opening_sequence: str
    through_sequence: str
    hand_id: str
    pending_returns: tuple[int, ...] | None

    @property
    def fees(self):
        return {
            player: self.opening_cash[i] - self.game.initial_stacks[i]
            for i, player in enumerate(self.player_ids)
        }


def _require(condition, reason):
    if not condition:
        raise ReplayUnavailable(reason)


def _actions(events):
    result = {}
    for event in events:
        if event.kind not in ("action_confirmed", "action_updated"):
            continue
        data = event.data
        old = result.get(data["source_event_id"])
        if old is None:
            _require(event.kind == "action_confirmed", "action_update_without_original")
            previous = next(reversed(result), None)
            _require(
                previous is None or int(data["source_event_id"]) == int(previous) + 1,
                "observed_action_sequence_gap",
            )
            result[data["source_event_id"]] = dict(data)
        else:
            for key, value in data.items():
                _require(
                    old[key] is None or value is None or old[key] == value,
                    "conflicting_observed_action",
                )
                if value is not None:
                    old[key] = value
    return result


def _opening(events, actions):
    fees, blinds, seen = {}, {}, set()
    for index, event in enumerate(events):
        if event.kind == "action_confirmed":
            data = actions[event.data["source_event_id"]]
            if data["source_event_id"] in seen:
                continue
            seen.add(data["source_event_id"])
            action, player = data["action"], data["player_id"]
            if action == "PAY_MUSHROOM":
                _require(not blinds and player not in fees, "invalid_fee_order")
                _require(data["amount_delta"] in (20, 40), "unsupported_mushroom_fee")
                fees[player] = data["amount_delta"]
            elif action in FORCED:
                _require(player in fees and action not in blinds, "unsupported_forced_bets")
                _require(data["amount_delta"] == FORCED[action], "unsupported_forced_bets")
                _require(data["amount_to"] in (None, FORCED[action]), "conflicting_forced_total")
                _require(tuple(blinds) == tuple(FORCED)[: len(blinds)], "invalid_blind_order")
                _require(action == tuple(FORCED)[len(blinds)], "invalid_blind_order")
                blinds[action] = player
            else:
                raise ReplayUnavailable("missing_opening_snapshot")
        elif event.kind == "snapshot":
            data = event.data
            # WP_dealNotify can still expose CLEAN; wait for its preflop snapshot.
            if not data["board"] and data["street"] in ("unknown", "between_hands"):
                continue
            _require(
                4 <= len(fees) <= 8 and set(blinds) == set(FORCED), "incomplete_opening_debits"
            )
            _require(
                not data["board"] and data["street"] == "preflop",
                "late_opening_snapshot",
            )
            _require(data["history_complete"], "incomplete_observed_history")
            people = {p["player_id"]: p for p in data["players"]}
            _require(set(fees) <= set(people), "missing_opening_players")
            players = tuple(sorted(fees, key=lambda p: people[p]["seat"]))
            _require(all(people[p]["stack"] is not None for p in players), "missing_opening_stacks")
            sb = players.index(blinds["SMALL_BLIND"])
            _require(len(set(blinds.values())) == 3, "conflicting_blind_players")
            _require(
                blinds["BIG_BLIND"] == players[(sb + 1) % len(players)]
                and blinds["THIRD_BLIND"] == players[(sb + 2) % len(players)],
                "blind_order_conflicts_with_seats",
            )
            _require(
                all(fee == (40 if p == players[sb] else 20) for p, fee in fees.items()),
                "fee_owner_conflicts_with_small_blind",
            )
            forced = {blinds[action]: amount for action, amount in FORCED.items()}
            after_fees = tuple(people[p]["stack"] + forced.get(p, 0) for p in players)
            game = PublicHoldemGame(
                HoldemRules(
                    after_fees,
                    small_blind=2,
                    big_blind=4,
                    straddle=8,
                    button=(sb - 1) % len(players),
                )
            )
            return (
                index,
                game,
                players,
                tuple(people[p]["seat"] for p in players),
                tuple(after_fees[i] + fees[p] for i, p in enumerate(players)),
                seen,
            )
        elif event.kind in ("board_updated", "settlement", "settlement_pending"):
            raise ReplayUnavailable("missing_opening_snapshot")
    raise ReplayUnavailable("missing_opening_snapshot")


def _snapshot(game, state, data, players, seats):
    _require(data["history_complete"], "incomplete_observed_history")
    people = {p["player_id"]: p for p in data["players"]}
    _require(set(players) <= set(people), "participant_disappeared")
    _require(tuple(people[p]["seat"] for p in players) == seats, "participant_changed_seat")
    if state.awaiting_settlement:
        _require(data["street"] in (state.street_name, "showdown"), "hand_not_active")
        _require(tuple(data["board"]) == state.board, "snapshot_board_conflict")
        _require(game.actor(state) is None and data["turn"] is None, "terminal_turn_conflict")
        return None  # Cash may already include observed payouts; the public game does not pay them.
    # Betting can close before WPK publishes the next street's observed board.
    previous_street = tuple(ROUNDS.values())[state.street - 1] if state.street else None
    _require(
        data["street"] == state.street_name
        or (state.pending_deal and data["street"] == previous_street),
        "snapshot_street_conflict",
    )
    _require(tuple(data["board"]) == state.board, "snapshot_board_conflict")
    _require(all(people[p]["stack"] is not None for p in players), "missing_snapshot_stacks")
    pending_returns = tuple(state.stacks[i] - people[p]["stack"] for i, p in enumerate(players))
    _require(
        not any(pending_returns)
        or (
            game.actor(state) == -1
            and sum(
                not folded and stack > 0
                for folded, stack in zip(state.folded, state.stacks, strict=True)
            )
            <= 1
            and all(
                delta in (0, returned)
                for delta, returned in zip(pending_returns, state.returned, strict=True)
            )
        ),
        "snapshot_cash_conflict",
    )
    if data["turn"] is not None:
        actor = game.actor(state)
        _require(
            actor not in (None, -1) and data["turn"]["player_id"] == players[actor],
            "snapshot_actor_conflict",
        )
        _require(
            data["turn"]["call_amount"]
            in (None, max(0, state.current_bet - state.street_bets[actor])),
            "snapshot_call_amount_conflict",
        )
    return pending_returns


def _observe_action(game, state, data, players):
    _require(data["player_id"] in players, "unpaid_player_acted")
    _require(
        ROUNDS.get(data["source_round"]) == state.street_name,
        "unknown_or_conflicting_action_street",
    )
    player = players.index(data["player_id"])
    kind = data["action"]
    if kind in ("FOLD", "CHECK", "CALL"):
        action = kind.lower()
    elif kind in ("BET", "RAISE", "ALL_IN"):
        if kind == "ALL_IN":
            _require(data["amount_delta"] == state.stacks[player], "all_in_cash_conflict")
        total = state.street_bets[player] + data["amount_delta"]
        if kind == "ALL_IN" and total <= state.current_bet:
            action = "call"
        else:
            _require(
                (kind != "BET" or state.current_bet == 0) and total > state.current_bet,
                "conflicting_bet_kind",
            )
            action = f"raise_to:{total}"
    else:
        raise ReplayUnavailable("unsupported_action_or_forced_bet")
    return game.observe_action(
        state,
        player=player,
        action=action,
        amount_delta=data["amount_delta"],
        amount_to=data["amount_to"],
    )


def replay_capture(events: Sequence[BusinessEvent | dict]) -> PublicReplay:
    """Validate one contiguous source prefix ending at the caller's requested head.

    Keep interleaved previous-hand settlements in the prefix so sequence gaps
    remain detectable. Only fields known by this prefix can repair an action.
    WPK can deliver a board before its preceding actions. Once those explicit
    actions arrive, replay them before that board, still checking each snapshot.
    """
    _require(bool(events) and len(events) <= 4096, "capture_prefix_unavailable")
    try:
        parsed = [
            e if isinstance(e, BusinessEvent) else BusinessEvent.model_validate(e) for e in events
        ]
    except ValueError as error:
        raise ReplayUnavailable("invalid_capture_event") from error
    _require(len({e.source_id for e in parsed}) == 1, "mixed_capture_sources")
    _require(
        all(
            int(b.sequence) == int(a.sequence) + 1 for a, b in zip(parsed, parsed[1:], strict=False)
        ),
        "capture_sequence_gap",
    )
    starts = [i for i, event in enumerate(parsed) if event.kind == "hand_started"]
    _require(bool(starts), "missing_hand_start")
    start = parsed[starts[-1]]
    _require(start.data["history_complete"], "incomplete_observed_history")
    suffix = parsed[starts[-1] :]
    _require(not any(e.kind == "capture_gap" for e in suffix), "capture_gap_after_opening")
    _require(not any(e.kind == "room_closed" for e in suffix), "room_closed")
    for event in suffix:
        _require(
            event.hand_id == start.hand_id or event.kind in ("settlement", "settlement_pending"),
            "hand_changed_without_start",
        )
    current = [e for e in suffix if e.hand_id == start.hand_id]
    _require(current[-1].kind == "snapshot", "awaiting_atomic_snapshot")
    actions = _actions(current)
    try:
        index, game, players, seats, cash, seen = _opening(current, actions)
        state = game.initial_state()
        pending_returns = (0,) * len(players)
        for event in current[index:]:
            if event.kind == "action_confirmed":
                data = actions[event.data["source_event_id"]]
                if data["source_event_id"] in seen:
                    continue
                seen.add(data["source_event_id"])
                state = _observe_action(game, state, data, players)
            elif event.kind == "board_updated":
                if len(event.data["cards"]) > len(state.board) and not state.pending_deal:
                    for action_id, data in actions.items():
                        if action_id in seen:
                            continue
                        _require(
                            ROUNDS.get(data["source_round"]) == state.street_name,
                            "awaiting_prior_street_actions",
                        )
                        state = _observe_action(game, state, data, players)
                        seen.add(action_id)
                        if state.pending_deal:
                            break
                    _require(bool(state.pending_deal), "awaiting_prior_street_actions")
                state = game.observe_board(state, event.data["cards"])
            elif event.kind == "snapshot":
                pending_returns = _snapshot(game, state, event.data, players, seats)
        return PublicReplay(
            game,
            state,
            players,
            seats,
            cash,
            current[index].sequence,
            parsed[-1].sequence,
            start.hand_id,
            pending_returns,
        )
    except ReplayUnavailable:
        raise
    except ValueError as error:
        raise ReplayUnavailable("illegal_observed_betting_history") from error
