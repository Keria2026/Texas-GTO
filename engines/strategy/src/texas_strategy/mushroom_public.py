"""Strict queries of an existing mushroom policy from fully specified public facts.

This does not reconstruct missing observations, sample opponents' cards, build a
public belief or solve a subgame. The service remains responsible for proving the
origin and timing of the supplied facts.
"""

from dataclasses import dataclass

from texas_game_core.cards import validate_cards
from texas_game_core.public_holdem import PublicHoldemGame, PublicHoldemState
from texas_strategy.mushroom_abstraction import (
    MushroomContextGame,
    context_action_map,
    encode_context,
)


@dataclass(frozen=True)
class PublicMushroomQuery:
    player: int
    information_key: str
    actions: tuple[str, ...]
    concrete_actions: tuple[str, ...]
    rules_version: str
    num_players: int

    @property
    def action_map(self) -> dict[str, str]:
        return dict(zip(self.actions, self.concrete_actions, strict=True))


def _seat(value, count, name):
    if type(value) is not int or not 0 <= value < count:
        raise ValueError(f"{name} must be an explicit seat within the table")


def _amounts(values, count, name):
    if (
        not isinstance(values, tuple)
        or len(values) != count
        or any(type(value) is not int or value < 0 for value in values)
    ):
        raise ValueError(f"{name} requires a known nonnegative integer for every seat")


def _seats(values, count, name):
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be an explicit tuple of seats")
    for value in values:
        _seat(value, count, name)
    if tuple(sorted(set(values))) != values:
        raise ValueError(f"{name} must be sorted without duplicates")


def _history_labels(game, state, participants, button, count):
    current = game.initial_state()
    labels = []

    def board_until_decision(current):
        while game.actor(current) == -1:
            if len(current.board) >= len(state.board):
                raise ValueError("Observed board is incomplete for the public history")
            current = game.transition(current, f"deal:{state.board[len(current.board)]}")
        return current

    for event in state.history:
        current = board_until_decision(current)
        if game.actor(current) != event.player or current.street_name != event.street:
            raise ValueError("Public action actor or street does not match replay")
        mapping = context_action_map(game, current)
        label = next(
            (name for name, action in mapping.items() if action == event.action), event.action
        )
        labels.append((current.street, (participants[event.player] - button) % count, label))
        current = game.transition(current, event.action)
        if current.history[-1] != event:
            raise ValueError("Public action amounts or abstraction marker do not match replay")
    current = board_until_decision(current)
    if current != state:
        raise ValueError("Public state does not match its complete action and board history")
    return tuple(labels)


def build_public_query(
    *,
    model: MushroomContextGame,
    hand_game: PublicHoldemGame,
    public_state: PublicHoldemState,
    table_button: int,
    policy_anchor: int,
    hand_offset: int,
    participants: tuple[int, ...],
    stacks: tuple[int, ...],
    outstanding: tuple[int, ...],
    pool: int,
    waiting_for_top_up: tuple[int, ...],
    hero_seat: int,
    hero_cards: tuple[str, str],
) -> PublicMushroomQuery:
    """Validate explicit facts and replay labels, then encode the unchanged model key.

    Physical seats are zero-based. ``hand_offset`` is zero-based within the
    supplied model's finite window, not a guessed real-session hand number.
    Table stacks and outstanding contributions include every stable table seat.
    Later-window ledger provenance requires external evidence; local validation
    alone cannot prove that earlier hands actually produced those balances.
    """
    if not isinstance(model, MushroomContextGame):
        raise ValueError("A bound MushroomContextGame model is required")
    if not isinstance(hand_game, PublicHoldemGame) or not isinstance(
        public_state, PublicHoldemState
    ):
        raise ValueError(
            "Public hand game and state are required; hidden-card states are forbidden"
        )
    count = model.num_players
    for name, seat in (
        ("table_button", table_button),
        ("policy_anchor", policy_anchor),
        ("hero_seat", hero_seat),
    ):
        _seat(seat, count, name)
    if policy_anchor != model.base_game.button:
        raise ValueError("Policy anchor does not match the supplied model")
    if type(hand_offset) is not int or not 0 <= hand_offset < model.base_game.horizon:
        raise ValueError("Explicit hand_offset must be within the model's finite window")
    if hand_offset == 0 and table_button != policy_anchor:
        raise ValueError("First window hand button must match the policy anchor")
    _amounts(stacks, count, "stacks")
    _amounts(outstanding, count, "outstanding")
    _seats(participants, count, "participants")
    _seats(waiting_for_top_up, count, "waiting_for_top_up")
    if (
        not 4 <= len(participants) <= count
        or set(participants) & set(waiting_for_top_up)
        or set(participants) | set(waiting_for_top_up) != set(range(count))
    ):
        raise ValueError("Every seat must be a participant or explicitly waiting for a top-up")
    if type(pool) is not int or pool < 0 or pool != sum(outstanding):
        raise ValueError("Pool must equal the complete outstanding contribution ledger")
    if (
        hand_game.num_players != len(participants)
        or hand_game.small_blind != 2
        or hand_game.big_blind != 4
        or hand_game.straddle != 8
        or hand_game.bet_fractions != (0.5, 1.0)
    ):
        raise ValueError("Public hand rules do not match the mushroom model")
    small_blind = next(
        (table_button + step) % count
        for step in range(1, count + 1)
        if (table_button + step) % count in participants
    )
    if hand_game.small_blind_player != participants.index(small_blind):
        raise ValueError("Public blind positions do not match the physical table button")
    hand_game.validate_state(public_state)
    actor = hand_game.actor(public_state)
    if actor in (None, -1) or participants[actor] != hero_seat:
        raise ValueError("The observed player must be the current acting participant")
    if tuple(stacks[seat] for seat in participants) != public_state.stacks:
        raise ValueError("Table and public hand stacks disagree")
    if sum(stacks) + pool + public_state.pot != model.base_game.funding_total:
        raise ValueError("Table chips, pot and escrow do not conserve the model's window funding")
    if hand_offset == 0:
        # This computes eligibility/fees/blinds only; no private card is generated.
        opening = model.base_game.initial_state().table
        opening_fees = dict(opening.ledger.snapshot().outstanding_contributions)
        if (
            opening.hand is None
            or participants != opening.participants
            or waiting_for_top_up != opening.waiting_for_top_up
            or pool != opening.pool
            or outstanding != tuple(opening_fees.get(str(seat), 0) for seat in range(count))
            or hand_game.initial_stacks != opening.hand.game.initial_stacks
            or any(stacks[seat] != opening.stacks[seat] for seat in waiting_for_top_up)
        ):
            raise ValueError("First-hand eligibility, funding or escrow does not match the model")
    for seat in participants:
        fee = 40 if seat == small_blind else 20
        if outstanding[seat] < fee:
            raise ValueError("Outstanding contributions omit the current hand's mushroom fee")
    if not isinstance(hero_cards, tuple) or len(hero_cards) != 2:
        raise ValueError("Exactly two known hero cards are required")
    if validate_cards(hero_cards + public_state.board) != hero_cards + public_state.board:
        raise ValueError("Visible cards must use canonical spelling")
    actions = _history_labels(hand_game, public_state, participants, table_button, count)
    mapping = context_action_map(hand_game, public_state)
    key = encode_context(
        num_players=count,
        physical=hero_seat,
        button=table_button,
        betting_round=4 * hand_offset + public_state.street,
        cards=hero_cards,
        hand=public_state,
        stacks=stacks,
        outstanding=outstanding,
        participants=participants,
        waiting_for_top_up=waiting_for_top_up,
        pool=pool,
        actions=actions,
        legal_actions=mapping,
    )
    return PublicMushroomQuery(
        (hero_seat - policy_anchor) % count,
        key,
        tuple(mapping),
        tuple(mapping.values()),
        model.rules_version,
        count,
    )


def lookup_public_policy(policy, query: PublicMushroomQuery):
    """Read the pinned policy; absent averages remain absent, never uniform advice."""
    metadata = policy.metadata
    if (
        metadata["game_id"] != "mushroom"
        or metadata["rules_version"] != query.rules_version
        or metadata["num_players"] != query.num_players
    ):
        raise ValueError("Policy snapshot does not match the public query's model")
    entry = policy.lookup(query.player, query.information_key)
    if entry is not None and entry.actions != query.actions:
        raise ValueError("Stored policy actions do not match the public state's legal actions")
    return entry
