"""An exact, replayable mushroom escrow for explicitly configured simulations.

This is a separate ledger, not a WPK rule implementation or a mushroom strategy.
The caller declares when a hand begins, its dealt participants and an eligible
sole winner. No hidden cards are accepted, inferred or exposed by this module.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace


class UnsupportedRuleError(ValueError):
    """The source rules do not resolve this case, or this simulator omits it."""


def _integer(value: int, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty identifier")


@dataclass(frozen=True)
class MushroomRules:
    """Policies with no verified live-platform default.

    Supported simulation choices: contribution_timing='before_hand',
    roster_policy='explicit_dealt_players', entry_policy='between_hands',
    insufficient_policy='reject', freeze_policy='no_transfers', and
    remainder_policy='participant_order' or 'reject'. None means unresolved.
    participant_order awards one leftover unit to each first-listed participant.
    The caller's roster order must therefore be stable (for example seat order).
    """

    base_points: int
    version: str
    minimum_players: int = 4
    small_blind_extra_units: int = 1
    contribution_timing: str | None = None
    roster_policy: str | None = None
    entry_policy: str | None = None
    insufficient_policy: str | None = None
    freeze_policy: str | None = None
    remainder_policy: str | None = None
    settlement_policy: str | None = None

    def __post_init__(self) -> None:
        _integer(self.base_points, "base_points", 1)
        _integer(self.minimum_players, "minimum_players", 2)
        _integer(self.small_blind_extra_units, "small_blind_extra_units", 1)
        _identifier(self.version, "version")

    def validate_hand_policies(self, timing: str) -> None:
        required = {
            "contribution_timing": "before_hand",
            "roster_policy": "explicit_dealt_players",
            "entry_policy": "between_hands",
            "insufficient_policy": "reject",
            "freeze_policy": "no_transfers",
        }
        for name, supported in required.items():
            if getattr(self, name) != supported:
                raise UnsupportedRuleError(f"Declare simulation policy {name}={supported!r}")
        if timing != self.contribution_timing:
            raise UnsupportedRuleError("Contribution event does not match declared timing")


@dataclass(frozen=True)
class MushroomEvent:
    event_id: str
    kind: str
    hand_id: str | None = None
    participants: tuple[str, ...] = ()
    small_blind: str | None = None
    available_chips: tuple[tuple[str, int], ...] = ()
    timing: str | None = None
    winner: str | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class MushroomState:
    rules_version: str
    pool: int = 0
    player_net: tuple[tuple[str, int], ...] = ()
    outstanding_contributions: tuple[tuple[str, int], ...] = ()
    frozen: bool = True
    hand_id: str | None = None
    small_blind: str | None = None
    last_participants: tuple[str, ...] = ()
    completed_hands: tuple[str, ...] = ()
    ended: bool = False
    version: int = 0

    def check_conservation(self) -> bool:
        """The pool balances all recorded contributions, including an opening anchor."""
        if isinstance(self.pool, bool) or not isinstance(self.pool, int) or self.pool < 0:
            return False
        if len({player for player, _ in self.player_net}) != len(self.player_net):
            return False
        if any(
            not isinstance(player, str)
            or not player.strip()
            or isinstance(value, bool)
            or not isinstance(value, int)
            for player, value in self.player_net
        ):
            return False
        contributions = dict(self.outstanding_contributions)
        if len(contributions) != len(self.outstanding_contributions) or any(
            not isinstance(player, str) or not player.strip() or type(value) is not int or value < 0
            for player, value in contributions.items()
        ):
            return False
        return (
            self.pool + sum(value for _, value in self.player_net) == 0
            and sum(contributions.values()) == self.pool
        )


@dataclass(frozen=True)
class MushroomTransition:
    state: MushroomState
    transfers: tuple[tuple[str, int], ...]
    reveal_required: tuple[str, ...] = ()
    duplicate: bool = False


def _finish(
    state: MushroomState, changes: Mapping[str, int], **updates: object
) -> MushroomTransition:
    net = dict(state.player_net)
    for player, amount in changes.items():
        net[player] = net.get(player, 0) + amount
    new = replace(
        state,
        pool=state.pool - sum(changes.values()),
        player_net=tuple(sorted(net.items())),
        version=state.version + 1,
        **updates,
    )
    if not new.check_conservation():
        raise ValueError("Invalid escrow state: player net plus pool does not conserve chips")
    return MushroomTransition(new, tuple(changes.items()))


def apply_mushroom_event(
    state: MushroomState, event: MushroomEvent, rules: MushroomRules
) -> MushroomTransition:
    """Pure reducer; use MushroomLedger for event-ID deduplication and replay.

    begin_hand is the only roster transition; mid-hand joins/leaves are omitted.
    A frozen hand records participants but makes no contributions or awards.
    end_session is an explicit caller event, never inferred from player count.
    """
    _identifier(event.event_id, "event_id")
    if state.rules_version != rules.version:
        raise ValueError("Rules version mismatch")
    if not state.check_conservation():
        raise ValueError("Invalid initial escrow state")
    if state.ended:
        raise ValueError("The session has ended")
    if event.kind == "begin_hand":
        rules.validate_hand_policies(event.timing)
        _identifier(event.hand_id, "hand_id")
        if state.hand_id is not None:
            raise ValueError("Settle the current hand before beginning another")
        if event.hand_id in state.completed_hands:
            raise ValueError("This hand has already been recorded")
        players = event.participants
        if len(players) < 2 or len(set(players)) != len(players):
            raise ValueError("At least two distinct dealt participants are required")
        for player in players:
            _identifier(player, "participant")
        if event.small_blind not in players:
            raise UnsupportedRuleError("A single explicitly identified small blind is required")
        chips = dict(event.available_chips)
        if len(chips) != len(event.available_chips) or set(chips) != set(players):
            raise ValueError("Available chips must cover exactly the dealt participants")
        for amount in chips.values():
            _integer(amount, "available_chips")
        frozen = len(players) < rules.minimum_players
        changes = {}
        if not frozen:
            for player in players:
                due = rules.base_points * (
                    1 + (rules.small_blind_extra_units if player == event.small_blind else 0)
                )
                if chips[player] < due:
                    raise UnsupportedRuleError("Insufficient chips: reject the entire contribution")
                changes[player] = -due
        outstanding = dict(state.outstanding_contributions)
        for player, amount in changes.items():
            outstanding[player] = outstanding.get(player, 0) - amount
        return _finish(
            state,
            changes,
            frozen=frozen,
            hand_id=event.hand_id,
            small_blind=event.small_blind,
            last_participants=players,
            outstanding_contributions=tuple(sorted(outstanding.items())),
        )
    if event.kind == "settle_hand":
        if event.hand_id is None or event.hand_id != state.hand_id:
            raise ValueError("Settlement must reference the open hand")
        if event.outcome not in {"sole_main_pot", "split_main_pot"}:
            raise UnsupportedRuleError(
                "Split pots, side pots and unknown award eligibility omitted"
            )
        if event.outcome == "sole_main_pot" and event.winner not in state.last_participants:
            raise ValueError("Winner must be a dealt participant")
        if event.outcome == "split_main_pot" and event.winner is not None:
            raise ValueError("Split main pot cannot declare a sole winner")
        award = (
            event.outcome == "sole_main_pot"
            and event.winner == state.small_blind
            and not state.frozen
            and state.pool > 0
        )
        result = _finish(
            state,
            {event.winner: state.pool} if award else {},
            hand_id=None,
            small_blind=None,
            completed_hands=(*state.completed_hands, event.hand_id),
            outstanding_contributions=() if award else state.outstanding_contributions,
        )
        return replace(result, reveal_required=(event.winner,) if award else ())
    if event.kind == "end_session":
        if state.hand_id is not None:
            raise ValueError("Settle the final hand before ending the session")
        if rules.settlement_policy == "refund_contributions":
            return _finish(
                state,
                dict(state.outstanding_contributions),
                outstanding_contributions=(),
                ended=True,
            )
        if rules.settlement_policy is not None:
            raise UnsupportedRuleError("Unsupported session settlement policy")
        if state.pool and not state.last_participants:
            raise UnsupportedRuleError("Final hand participants are unknown")
        if rules.remainder_policy not in {"participant_order", "reject"}:
            raise UnsupportedRuleError("Declare a simulation remainder_policy before session end")
        changes = {}
        if state.last_participants:
            each, remainder = divmod(state.pool, len(state.last_participants))
            if remainder and rules.remainder_policy == "reject":
                raise UnsupportedRuleError("Pool cannot be divided exactly under reject policy")
            changes = {
                player: each + int(index < remainder)
                for index, player in enumerate(state.last_participants)
            }
        return _finish(state, changes, outstanding_contributions=(), ended=True)
    raise ValueError(f"Unknown mushroom event kind: {event.kind!r}")


class MushroomLedger:
    """In-memory event log replayed from its explicit opening contributions.

    Transfers are deltas in this separate escrow ledger. If another account
    source already includes them, do not add these deltas to that source again.
    Opening contributions are already paid, not new transfer events. Persist
    both the opening anchor and subsequent events. Duplicates return no transfers.
    """

    def __init__(
        self, rules: MushroomRules, *, opening_contributions: Mapping[str, int] | None = None
    ) -> None:
        self.rules = rules
        contributions = {}
        if opening_contributions is not None:
            if not isinstance(opening_contributions, Mapping):
                raise ValueError("Opening contributions must map players to paid amounts")
            for player, amount in opening_contributions.items():
                _identifier(player, "opening contributor")
                _integer(amount, "opening contribution")
                if amount:
                    contributions[player] = amount
        outstanding = tuple(sorted(contributions.items()))
        self._opening_state = MushroomState(
            rules_version=rules.version,
            pool=sum(contributions.values()),
            player_net=tuple((player, -amount) for player, amount in outstanding),
            outstanding_contributions=outstanding,
        )
        self._state = self._opening_state
        self._events: dict[str, MushroomEvent] = {}

    @property
    def events(self) -> tuple[MushroomEvent, ...]:
        return tuple(self._events.values())

    def snapshot(self) -> MushroomState:
        return self._state

    def fork(self) -> "MushroomLedger":
        candidate = MushroomLedger(self.rules)
        candidate._opening_state = self._opening_state
        candidate._state = self._state
        candidate._events = self._events.copy()
        return candidate

    def record(self, event: MushroomEvent) -> MushroomTransition:
        previous = self._events.get(event.event_id)
        if previous is not None:
            if previous != event:
                raise ValueError("Conflicting payload for an existing event_id")
            return MushroomTransition(self._state, (), duplicate=True)
        result = apply_mushroom_event(self._state, event, self.rules)
        self._events[event.event_id] = event
        self._state = result.state
        return result

    def rebuild(self, events: Sequence[MushroomEvent]) -> MushroomState:
        candidate = MushroomLedger(
            self.rules,
            opening_contributions=dict(self._opening_state.outstanding_contributions),
        )
        for event in events:
            candidate.record(event)
        self._events, self._state = candidate._events, candidate._state
        return self._state

    def begin_hand(
        self,
        event_id: str,
        hand_id: str,
        participants: Sequence[str],
        small_blind: str,
        available_chips: Mapping[str, int],
        *,
        timing: str,
    ) -> MushroomTransition:
        return self.record(
            MushroomEvent(
                event_id=event_id,
                kind="begin_hand",
                hand_id=hand_id,
                participants=tuple(participants),
                small_blind=small_blind,
                available_chips=tuple(sorted(available_chips.items())),
                timing=timing,
            )
        )

    def settle_hand(
        self, event_id: str, hand_id: str, winner: str | None, *, outcome: str
    ) -> MushroomTransition:
        return self.record(
            MushroomEvent(
                event_id=event_id,
                kind="settle_hand",
                hand_id=hand_id,
                winner=winner,
                outcome=outcome,
            )
        )

    def end_session(self, event_id: str) -> MushroomTransition:
        return self.record(MushroomEvent(event_id=event_id, kind="end_session"))
