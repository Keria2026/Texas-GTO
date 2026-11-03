"""Strict WPK/1 observation contract, matching TGTO capture-core ABI 3.

Observed options are not engine-verified legal actions. Unknown chips remain null.
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA = "tgto.capture/0.1"
CAPABILITY = "capture_events_v1"
STATE_CAPABILITY = "capture_state_v1"
STATE_SCHEMA = "tgto.capture-state/0.1"
SAFE = (1 << 53) - 1
U64 = (1 << 64) - 1
Chips = Annotated[int, Field(ge=0, le=SAFE)]
SignedChips = Annotated[int, Field(ge=-SAFE, le=SAFE)]
Card = Annotated[str, Field(pattern=r"^[A23456789TJQK][shcd]$")]
Street = Literal["preflop", "flop", "turn", "river", "showdown", "between_hands", "unknown"]
Choice = Literal["FOLD", "CHECK", "CALL", "BET", "RAISE", "ALL_IN"]


def decimal(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value) or int(value) > U64:
        raise ValueError("Expected canonical positive u64")
    return value


def alias(value: str) -> str:
    if value != "hero":
        if not value.startswith("player-"):
            raise ValueError("Expected scoped player alias")
        decimal(value[7:])
    return value


def table(value: str) -> str:
    if not value.startswith("table-"):
        raise ValueError("Expected scoped table alias")
    decimal(value[6:])
    return value


def unique_cards(cards):
    known = [c for c in cards if c is not None]
    if len(known) != len(set(known)):
        raise ValueError("Duplicate visible cards")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Player(Strict):
    player_id: str
    seat: Annotated[int, Field(ge=1, le=8)]
    stack: Chips | None
    _alias = field_validator("player_id")(alias)


class Players(Strict):
    players: Annotated[list[Player], Field(max_length=8)]

    @model_validator(mode="after")
    def unique(self):
        for key in ("player_id", "seat"):
            values = [getattr(p, key) for p in self.players]
            if len(values) != len(set(values)):
                raise ValueError("Duplicate player/seat")
        return self


class Turn(Strict):
    player_id: str
    legal_actions: Annotated[list[Choice], Field(max_length=6)]
    call_amount: Chips | None
    min_raise_reported: Chips | None
    max_raise_reported: Chips | None
    _alias = field_validator("player_id")(alias)

    @model_validator(mode="after")
    def private_options(self):
        if self.player_id != "hero" and (
            self.legal_actions
            or any(
                v is not None
                for v in (self.call_amount, self.min_raise_reported, self.max_raise_reported)
            )
        ):
            raise ValueError("Opponent private options forbidden")
        return self


class Action(Strict):
    source_event_id: str
    player_id: str
    action: Literal[
        "PAY_MUSHROOM",
        "SMALL_BLIND",
        "BIG_BLIND",
        "THIRD_BLIND",
        "ANTE",
        "BET",
        "RAISE",
        "CALL",
        "ALL_IN",
        "FOLD",
        "CHECK",
    ]
    amount_delta: Chips
    amount_to: Chips | None
    source_round: Annotated[str, Field(max_length=32)] | None
    _id = field_validator("source_event_id")(decimal)
    _alias = field_validator("player_id")(alias)

    @model_validator(mode="after")
    def amounts(self):
        if self.action in ("FOLD", "CHECK") and self.amount_delta != 0:
            raise ValueError("Fold/check cannot debit chips")
        if self.source_round is not None and len(self.source_round.encode()) > 32:
            raise ValueError("Source round too long")
        return self


class VisibleCards(Strict):
    hero_cards: Annotated[list[Card | None], Field(min_length=2, max_length=2)]
    board: Annotated[list[Card], Field(max_length=5)]

    @model_validator(mode="after")
    def cards_valid(self):
        if len(self.board) not in (0, 3, 4, 5):
            raise ValueError("Invalid board size")
        unique_cards(self.hero_cards + self.board)
        return self


class Snapshot(Players, VisibleCards):
    street: Street
    turn: Turn | None
    history_complete: bool
    rules_version: Literal["wpk-observed-unverified-v1"]

    @model_validator(mode="after")
    def turn_street(self):
        if self.street not in ("preflop", "flop", "turn", "river") and self.turn is not None:
            raise ValueError("Turn outside an action street")
        return self


class HandStarted(Strict):
    hand_number: Annotated[int, Field(ge=1, le=SAFE)]
    history_complete: bool


class HeroCards(Strict):
    cards: Annotated[list[Card | None], Field(min_length=2, max_length=2)]
    _unique = field_validator("cards")(lambda v: unique_cards(v) or v)


class Board(Strict):
    cards: Annotated[list[Card], Field(max_length=5)]

    @field_validator("cards")
    @classmethod
    def validate_cards(cls, cards):
        if len(cards) not in (0, 3, 4, 5):
            raise ValueError("Invalid board size")
        unique_cards(cards)
        return cards


class Requested(Strict):
    turn: Turn | None


class ResultPlayer(Strict):
    player_id: str
    net_chips: SignedChips | None
    mushroom_paid: Chips | None
    mushroom_awarded: Chips | None
    _alias = field_validator("player_id")(alias)


class ResultPlayers(Strict):
    players: Annotated[list[ResultPlayer], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def unique(self):
        ids = [p.player_id for p in self.players]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate settlement player")
        return self


class SettlementPending(ResultPlayers):
    received: Literal["result", "log"]


class Settlement(ResultPlayers, VisibleCards):
    table_id: str
    hand_number: Annotated[int, Field(ge=1, le=SAFE)]
    status: Literal["reconciled", "unresolved"]
    reason: Literal["point_difference", "missing_amounts"] | None
    pool_change: SignedChips | None
    _table = field_validator("table_id")(table)

    @model_validator(mode="after")
    def conservation(self):
        if self.status == "unresolved":
            if self.reason is None:
                raise ValueError("Unresolved settlement needs a reason")
            return self
        if self.reason is not None or any(
            v is None
            for p in self.players
            for v in (p.net_chips, p.mushroom_paid, p.mushroom_awarded)
        ):
            raise ValueError("Incomplete reconciled settlement")
        pool = sum(p.mushroom_paid - p.mushroom_awarded for p in self.players)
        if sum(p.net_chips for p in self.players) + pool != 0 or self.pool_change != pool:
            raise ValueError("Settlement does not conserve chips")
        return self


class Closed(Strict):
    reconciled: Literal[False]

    @field_validator("reconciled", mode="before")
    @classmethod
    def boolean(cls, value):
        if value is not False:
            raise ValueError("Expected false")
        return value


class Gap(Strict):
    reason: Annotated[str, Field(max_length=512)]

    @field_validator("reason")
    @classmethod
    def bytes_limit(cls, value):
        if len(value.encode()) > 512:
            raise ValueError("Gap reason too long")
        return value


DATA = {
    "snapshot": Snapshot,
    "hand_started": HandStarted,
    "hero_cards": HeroCards,
    "board_updated": Board,
    "players_updated": Players,
    "action_confirmed": Action,
    "action_updated": Action,
    "action_requested": Requested,
    "settlement_pending": SettlementPending,
    "settlement": Settlement,
    "room_closed": Closed,
    "capture_gap": Gap,
}


class BusinessEvent(Strict):
    wire_schema: Literal["tgto.capture/0.1"] = Field(alias="schema")
    type: Literal["business_event"]
    adapter_version: Literal["wpk/1"]
    source_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9-]{1,80}$")]
    sequence: str
    captured_at: Annotated[str, Field(max_length=40)]
    kind: str
    table_id: str | None
    hand_id: Annotated[str, Field(max_length=64)] | None
    data: dict
    _sequence = field_validator("sequence")(decimal)

    @field_validator("captured_at")
    @classmethod
    def timestamp(cls, value):
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("Timezone required")
        return value

    @model_validator(mode="after")
    def payload(self):
        model = DATA.get(self.kind)
        if model is None:
            raise ValueError("Unknown WPK event kind")
        parsed = model.model_validate(self.data)
        if self.table_id is not None:
            table(self.table_id)
        if self.hand_id is not None:
            prefix, sep, number = self.hand_id.partition(":")
            if not sep or prefix != self.table_id:
                raise ValueError("Hand/table mismatch")
            decimal(number)
        if self.kind != "capture_gap" and (self.table_id is None or self.hand_id is None):
            raise ValueError("Missing hand scope")
        if self.kind in ("hand_started", "settlement"):
            if self.hand_id != f"{self.table_id}:{parsed.hand_number}":
                raise ValueError("Hand number mismatch")
        if self.kind == "settlement" and parsed.table_id != self.table_id:
            raise ValueError("Settlement table mismatch")
        return self


class BusinessBatch(Strict):
    wire_schema: Literal["tgto.capture/0.1"] = Field(alias="schema")
    type: Literal["capture.append"]
    source_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9-]{1,80}$")]
    events: Annotated[list[BusinessEvent], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def contiguous(self):
        previous = None
        for event in self.events:
            if event.source_id != self.source_id or (
                previous is not None and int(event.sequence) != previous + 1
            ):
                raise ValueError("Noncontiguous business source")
            previous = int(event.sequence)
        return self
