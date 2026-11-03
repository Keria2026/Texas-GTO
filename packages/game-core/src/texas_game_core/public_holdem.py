"""Observed public betting replay; private cards are never filled in or sampled.

Starting stacks are available betting chips (after any external mushroom fee).
The caller must establish the actual dealt roster, button and completed private
deal before calling initial_state. This module cannot establish their provenance.
Showdown ends at an unpaid, awaiting-settlement state, not an invented winner.
"""

from collections.abc import Sequence
from dataclasses import KW_ONLY, dataclass, fields, replace

from texas_game_core.cards import normalize_card, validate_cards
from texas_game_core.holdem import STREETS, HoldemAction, HoldemGame


@dataclass(frozen=True, slots=True)
class HoldemRules:
    stacks: tuple[int, ...]
    _: KW_ONLY
    small_blind: int
    big_blind: int
    straddle: int | None
    button: int
    bet_fractions: tuple[float, ...] = (0.5, 1.0)

    def __post_init__(self):
        object.__setattr__(self, "stacks", tuple(self.stacks))
        object.__setattr__(self, "bet_fractions", tuple(self.bet_fractions))
        if any(type(v) not in (int, float) for v in self.bet_fractions):
            raise ValueError("Bet fractions must be numeric")
        self.game()  # Reuse the same rule validation and version encoding as training.

    def game(self) -> HoldemGame:
        return HoldemGame(
            self.stacks,
            small_blind=self.small_blind,
            big_blind=self.big_blind,
            straddle=self.straddle,
            button=self.button,
            bet_fractions=self.bet_fractions,
        )


@dataclass(frozen=True, slots=True)
class PublicHoldemState:
    rules_version: str
    stacks: tuple[int, ...]
    contributions: tuple[int, ...]
    street_bets: tuple[int, ...]
    board: tuple[str, ...]
    folded: tuple[bool, ...]
    street: int
    current_bet: int
    last_full_raise: int
    acted_to: tuple[int | None, ...]
    pending: tuple[int, ...]
    pending_deal: tuple[int, ...]
    to_act: int | None
    history: tuple[HoldemAction, ...] = ()
    returned: tuple[int, ...] = ()
    awaiting_settlement: bool = False

    @property
    def pot(self) -> int:
        return sum(self.contributions)

    @property
    def street_name(self) -> str:
        return STREETS[self.street]

    @property
    def terminal(self) -> bool:
        """No more decisions; funds still await an observed settlement."""
        return self.awaiting_settlement


class PublicHoldemGame(HoldemGame):
    """Reuse exact betting transitions, reopening and uncalled-return rules.

    The separate state/validator never relaxes HoldemGame's private-card checks.
    Validation checks structure/accounting; callers establish reachability by
    replaying the complete observed prefix, not by trusting arbitrary state fields.
    """

    def __init__(self, rules: HoldemRules):
        if type(rules) is not HoldemRules:
            raise ValueError("Explicit HoldemRules are required")
        super().__init__(
            rules.stacks,
            small_blind=rules.small_blind,
            big_blind=rules.big_blind,
            straddle=rules.straddle,
            button=rules.button,
            bet_fractions=rules.bet_fractions,
        )
        self._opening_contributions = super().initial_state().contributions

    def initial_state(self) -> PublicHoldemState:
        initial = super().initial_state()
        data = {
            field.name: getattr(initial, field.name)
            for field in fields(PublicHoldemState)
            if field.name != "awaiting_settlement"
        }
        # The explicit dealt-roster boundary acknowledges dealing, not any private card value.
        data["pending_deal"] = ()
        state = self._continue(PublicHoldemState(**data))
        self.validate_state(state)
        return state

    def validate_state(self, state: PublicHoldemState) -> None:
        if type(state) is not PublicHoldemState:
            raise ValueError("Expected a PublicHoldemState")
        if type(state.rules_version) is not str:
            raise ValueError("Invalid public rules version")
        self._check_game(state)
        vectors = (
            state.stacks,
            state.contributions,
            state.street_bets,
            state.folded,
            state.acted_to,
            state.returned,
        )
        if any(type(v) is not tuple or len(v) != self.num_players for v in vectors):
            raise ValueError("Public state vectors must match the dealt roster")
        for amounts in (state.stacks, state.contributions, state.street_bets, state.returned):
            if any(type(v) is not int or v < 0 for v in amounts):
                raise ValueError("Public chip amounts must be nonnegative integers")
        if any(type(v) is not bool for v in state.folded) or all(state.folded):
            raise ValueError("Invalid public fold status")
        if type(state.street) is not int or not 0 <= state.street <= 3:
            raise ValueError("Invalid public betting street")
        if type(state.board) is not tuple or any(type(card) is not str for card in state.board):
            raise ValueError("Public board must contain canonical cards")
        if validate_cards(state.board) != state.board:
            raise ValueError("Public board must contain canonical cards")
        if type(state.pending_deal) is not tuple or any(
            type(recipient) is not int or recipient != -1 for recipient in state.pending_deal
        ):
            raise ValueError("Only observed public board cards may be pending")
        if len(state.board) + len(state.pending_deal) != (0, 3, 4, 5)[state.street]:
            raise ValueError("Public board does not match the betting street")
        if (
            type(state.current_bet) is not int
            or state.current_bet < max(state.street_bets)
            or type(state.last_full_raise) is not int
            or state.last_full_raise < self.big_blind
        ):
            raise ValueError("Invalid public wager or full raise increment")
        if any(v is not None and (type(v) is not int or v < 0) for v in state.acted_to):
            raise ValueError("Invalid public acted-to amount")
        if (
            type(state.pending) is not tuple
            or any(type(p) is not int or not 0 <= p < self.num_players for p in state.pending)
            or len(set(state.pending)) != len(state.pending)
        ):
            raise ValueError("Invalid public pending order")
        if state.pending and state.pending != tuple(
            p for p in self._clockwise(state.pending[0]) if p in state.pending
        ):
            raise ValueError("Pending players must remain in clockwise order")
        if state.to_act is not None and (
            type(state.to_act) is not int or not 0 <= state.to_act < self.num_players
        ):
            raise ValueError("Invalid public actor")
        if type(state.awaiting_settlement) is not bool:
            raise ValueError("Invalid public settlement flag")
        if state.awaiting_settlement:
            if state.to_act is not None or state.pending or state.pending_deal:
                raise ValueError("Settlement cannot have pending decisions")
            if sum(not folded for folded in state.folded) > 1 and state.street != 3:
                raise ValueError("A contested showdown requires the complete river")
        elif state.pending_deal:
            if state.to_act is not None:
                raise ValueError("Players cannot act while the public board is pending")
            if (
                state.current_bet
                or any(state.street_bets)
                or any(v is not None for v in state.acted_to)
                or state.last_full_raise != self.big_blind
            ):
                raise ValueError("New public street must begin without previous wagers")
        elif (
            not state.pending
            or state.to_act != state.pending[0]
            or any(state.folded[p] or state.stacks[p] == 0 for p in state.pending)
        ):
            raise ValueError("Invalid public acting player")
        if type(state.history) is not tuple:
            raise ValueError("Public action history must be immutable")
        paid = list(self._opening_contributions)
        street_bets = list(self._opening_contributions)
        previous_street = 0
        for action in state.history:
            if (
                type(action) is not HoldemAction
                or type(action.street) is not str
                or action.street not in STREETS
                or type(action.player) is not int
                or not 0 <= action.player < self.num_players
                or type(action.action) is not str
                or type(action.amount_delta) is not int
                or action.amount_delta < 0
                or type(action.amount_to) is not int
                or action.amount_to < 0
                or type(action.in_action_abstraction) is not bool
            ):
                raise ValueError("Invalid public history record")
            street = STREETS.index(action.street)
            if not previous_street <= street <= state.street:
                raise ValueError("Public history streets must be ordered")
            if street != previous_street:
                street_bets = [0] * self.num_players
            previous_street = street
            if action.amount_to != street_bets[action.player] + action.amount_delta:
                raise ValueError("Public history total does not match its chip debit")
            street_bets[action.player] = action.amount_to
            if action.action in ("fold", "check"):
                if action.amount_delta:
                    raise ValueError("Fold/check cannot debit chips")
            elif action.action != "call" and not (
                action.action.startswith("raise_to:")
                and action.action.removeprefix("raise_to:").isascii()
                and action.action.removeprefix("raise_to:").isdecimal()
                and action.action == f"raise_to:{action.amount_to}"
            ):
                raise ValueError("Invalid public history action")
            paid[action.player] += action.amount_delta
        for player in range(self.num_players):
            if (
                state.street_bets[player] > state.contributions[player]
                or state.stacks[player] + state.contributions[player] != self.initial_stacks[player]
                or paid[player] - state.returned[player] != state.contributions[player]
            ):
                raise ValueError("Public chip accounting is inconsistent")

    def legal_actions(self, state: PublicHoldemState) -> tuple[str, ...]:
        self.validate_state(state)
        return (
            () if state.pending_deal or state.awaiting_settlement else super().legal_actions(state)
        )

    def transition(self, state: PublicHoldemState, action: str) -> PublicHoldemState:
        result = super().transition(state, action)
        self.validate_state(result)
        return result

    def observe_action(
        self,
        state: PublicHoldemState,
        *,
        player: int,
        action: str,
        amount_delta: int,
        amount_to: int | None = None,
    ) -> PublicHoldemState:
        self.validate_state(state)
        if (
            type(player) is not int
            or not 0 <= player < self.num_players
            or player != self.actor(state)
        ):
            raise ValueError("Observed player is not the current actor")
        if (
            type(amount_delta) is not int
            or amount_delta < 0
            or (amount_to is not None and (type(amount_to) is not int or amount_to < 0))
        ):
            raise ValueError("Observed amounts must be nonnegative integers")
        result = self.transition(state, action)
        recorded = result.history[-1]
        # A single action may immediately return uncalled chips; compare the debit, not net stack.
        if recorded.amount_delta != amount_delta or (
            amount_to is not None and recorded.amount_to != amount_to
        ):
            raise ValueError("Observed amount does not match the legal action")
        return result

    def observe_board(
        self,
        state: PublicHoldemState,
        cards: Sequence[str],
    ) -> PublicHoldemState:
        self.validate_state(state)
        if not isinstance(cards, (tuple, list)) or len(cards) not in (0, 3, 4, 5):
            raise ValueError("Observed board must contain 0, 3, 4 or 5 cards")
        board = tuple(cards)
        if any(type(card) is not str for card in board) or validate_cards(board) != board:
            raise ValueError("Observed board must contain distinct canonical cards")
        if board[: len(state.board)] != state.board:
            raise ValueError("Observed board cannot regress or change known cards")
        result = state
        for card in board[len(state.board) :]:
            if not result.pending_deal:
                raise ValueError("Public board arrived before the required player actions")
            result = self.transition(result, f"deal:{card}")
        return result

    def _deal(self, state: PublicHoldemState, action: str) -> PublicHoldemState:
        if not action.startswith("deal:") or not state.pending_deal:
            raise ValueError("An explicitly observed board card is required")
        card = normalize_card(action.removeprefix("deal:"))
        if action != f"deal:{card}" or card in state.board:
            raise ValueError("Public card must be canonical and not previously observed")
        result = replace(state, board=state.board + (card,), pending_deal=state.pending_deal[1:])
        return result if result.pending_deal else self._continue(result)

    def _settle(self, state: PublicHoldemState) -> PublicHoldemState:
        state = self._return_uncalled(state)
        return replace(state, pending=(), pending_deal=(), to_act=None, awaiting_settlement=True)

    def chance_outcomes(self, state):
        raise ValueError("Public replay requires observed cards; it never invents a deal")

    def sample_chance_outcome(self, state, rng):
        raise ValueError("Public replay cannot sample unobserved cards")

    def information_key(self, state, player):
        raise ValueError("Use a separate observed context with the actual hero cards")

    def terminal_payoffs(self, state):
        raise ValueError("Public replay awaits actual settlement; payoffs are unknown")
