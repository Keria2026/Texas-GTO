"""Confirmed mushroom transfers around a funded 4–8 seat Hold'em hand.

This building block keeps table chips and escrow together. It does not assign a
continuation value to a carried pool or resolve session-end/seat-removal rules.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from random import Random

from texas_game_core.holdem import HoldemGame, HoldemState
from texas_game_core.mushroom import UnsupportedRuleError


@dataclass(frozen=True)
class MushroomHand:
    game: HoldemGame
    state: HoldemState
    starting_stacks: tuple[int, ...]
    opening_pool: int
    fees: tuple[int, ...]

    @classmethod
    def start(cls, stacks: Sequence[int], *, button: int = 0, pool: int = 0) -> "MushroomHand":
        stacks = tuple(stacks)
        if not 4 <= len(stacks) <= 8:
            raise ValueError("Mushroom hands require 4 to 8 dealt players")
        if any(type(value) is not int or value < 0 for value in stacks):
            raise ValueError("Table stacks must be nonnegative integer points")
        if type(button) is not int or not 0 <= button < len(stacks):
            raise ValueError("Button must be a participating seat")
        if type(pool) is not int or pool < 0:
            raise ValueError("Mushroom pool must be nonnegative integer points")
        small_blind = (button + 1) % len(stacks)
        fees = tuple(40 if seat == small_blind else 20 for seat in range(len(stacks)))
        if any(stack <= fee for stack, fee in zip(stacks, fees, strict=True)):
            raise UnsupportedRuleError(
                "Resolve fee eligibility and seat removal before constructing a dealt hand"
            )
        after_fees = tuple(stack - fee for stack, fee in zip(stacks, fees, strict=True))
        # Fee transfers precede all forced wagers; the Holdem engine sees only
        # the actual amount available to bet, never the escrow balance.
        game = HoldemGame(after_fees, small_blind=2, big_blind=4, straddle=8, button=button)
        return cls(game, game.initial_state(), stacks, pool, fees)

    @property
    def award_player(self) -> int | None:
        if self.state.terminal and self.state.side_pots:
            winners = self.state.side_pots[0].winners
            if winners == (self.game.small_blind_player,):
                return winners[0]
        return None

    @property
    def award(self) -> int:
        return self.opening_pool + sum(self.fees) if self.award_player is not None else 0

    @property
    def pool(self) -> int:
        return self.opening_pool + sum(self.fees) - self.award

    @property
    def table_stacks(self) -> tuple[int, ...]:
        return tuple(
            amount + (self.award if player == self.award_player else 0)
            for player, amount in enumerate(self.state.stacks)
        )

    @property
    def reveal_required(self) -> tuple[int, ...]:
        return (self.award_player,) if self.award_player is not None else ()

    def transition(self, action: str) -> "MushroomHand":
        return replace(self, state=self.game.transition(self.state, action))

    def deal_to_decision(
        self, *, rng: Random | None = None, deck: Sequence[str] | None = None
    ) -> "MushroomHand":
        return replace(self, state=self.game.deal_to_decision(self.state, rng=rng, deck=deck))

    def next_hand(self, *, button: int) -> "MushroomHand":
        if not self.state.terminal:
            raise ValueError("Settle this hand before starting another")
        return self.start(self.table_stacks, button=button, pool=self.pool)

    def net_changes(self) -> tuple[int, ...]:
        if not self.state.terminal:
            raise ValueError("Net changes are available only after settlement")
        return tuple(
            end - start for end, start in zip(self.table_stacks, self.starting_stacks, strict=True)
        )

    def validate(self) -> None:
        self.game.validate_state(self.state)
        total = sum(self.table_stacks) + self.pool + self.state.pot
        if total != sum(self.starting_stacks) + self.opening_pool:
            raise ValueError("Mushroom pool plus table chips do not conserve points")
