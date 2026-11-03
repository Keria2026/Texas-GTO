"""Target mushroom session accounting over stable seats and actual chip balances.

This simulation coordinator never fills in missing live observations. Players
unable to fund entry wait for an explicit top-up; re-straddle is not exposed.
"""

from collections.abc import Sequence
from copy import copy
from random import Random

from texas_game_core.mushroom import MushroomLedger, MushroomRules
from texas_game_core.mushroom_hand import MushroomHand


class MushroomTable:
    def __init__(
        self,
        stacks: Sequence[int] = (800,) * 8,
        *,
        button: int = 0,
        outstanding: Sequence[int] | None = None,
        waiting_for_top_up: Sequence[int] = (),
    ):
        """Start between hands, before the next fees, with explicit existing escrow."""
        self.stacks = tuple(stacks)
        if not 4 <= len(self.stacks) <= 8 or any(
            type(value) is not int or value < 0 for value in self.stacks
        ):
            raise ValueError("Mushroom table needs 4–8 nonnegative integer seat balances")
        if type(button) is not int or not 0 <= button < len(self.stacks):
            raise ValueError("Button is outside the table")
        if outstanding is None:
            outstanding = (0,) * len(self.stacks)
        if (
            not isinstance(outstanding, Sequence)
            or len(outstanding) != len(self.stacks)
            or any(type(value) is not int or value < 0 for value in outstanding)
        ):
            raise ValueError("Outstanding contributions need a nonnegative integer for every seat")
        if not isinstance(waiting_for_top_up, Sequence):
            raise ValueError("Waiting seats must be an explicit sequence")
        waiting = tuple(waiting_for_top_up)
        if any(
            type(seat) is not int or not 0 <= seat < len(self.stacks) for seat in waiting
        ) or waiting != tuple(sorted(set(waiting))):
            raise ValueError("Waiting seats must be sorted, unique and within the table")
        self.button = button
        self.funding_total = sum(self.stacks) + sum(outstanding)
        self.participants: tuple[int, ...] = ()
        self.waiting_for_top_up: tuple[int, ...] = waiting
        self.hand: MushroomHand | None = None
        self.hand_number = 0
        self.ledger = MushroomLedger(
            MushroomRules(
                base_points=20,
                version="wpk-mushroom-fee-refund-v1",
                contribution_timing="before_hand",
                roster_policy="explicit_dealt_players",
                entry_policy="between_hands",
                insufficient_policy="reject",
                freeze_policy="no_transfers",
                settlement_policy="refund_contributions",
            ),
            opening_contributions={
                str(seat): amount for seat, amount in enumerate(outstanding) if amount
            },
        )
        self.validate()

    @property
    def ended(self) -> bool:
        return self.ledger.snapshot().ended

    @property
    def pool(self) -> int:
        return self.ledger.snapshot().pool

    def fork(self) -> "MushroomTable":
        candidate = copy(self)
        candidate.ledger = self.ledger.fork()
        return candidate

    def end_session(self) -> None:
        if self.ended:
            return
        if self.hand is not None and not self.hand.state.terminal:
            raise ValueError("Settle the open hand before ending the session")
        result = self.ledger.end_session(f"end:{self.hand_number}")
        balances = list(self.stacks)
        for player, amount in result.transfers:
            balances[int(player)] += amount
        self.stacks = tuple(balances)
        self.participants = ()
        self.validate()

    def _clockwise(self, after: int) -> tuple[int, ...]:
        return tuple(
            (after + offset) % len(self.stacks) for offset in range(1, len(self.stacks) + 1)
        )

    def begin_hand(self) -> MushroomHand | None:
        if self.ended:
            raise ValueError("Session has ended")
        if self.hand is not None and not self.hand.state.terminal:
            raise ValueError("Settle the open hand first")
        waiting = set(self.waiting_for_top_up)
        waiting.update(seat for seat, stack in enumerate(self.stacks) if stack <= 20)
        eligible = set(range(len(self.stacks))) - waiting
        while len(eligible) >= 4:
            order = tuple(seat for seat in self._clockwise(self.button) if seat in eligible)
            small_blind, _, straddle = order[:3]
            if self.stacks[small_blind] <= 40:
                waiting.add(small_blind)
                eligible.remove(small_blind)
            elif self.stacks[straddle] < 28:
                waiting.add(straddle)
                eligible.remove(straddle)
            else:
                break
        if len(eligible) < 4:
            self.waiting_for_top_up = tuple(sorted(waiting))
            self.end_session()
            return None
        participants = tuple(sorted(eligible))
        # With a removed dealer, the live seat immediately before the new SB
        # supplies the same action/deal order to the compact Holdem engine.
        local_button = (participants.index(small_blind) - 1) % len(participants)
        candidate = MushroomHand.start(
            tuple(self.stacks[seat] for seat in participants), button=local_button, pool=self.pool
        )
        hand_id = str(self.hand_number + 1)
        self.ledger.begin_hand(
            f"begin:{hand_id}",
            hand_id,
            tuple(map(str, participants)),
            str(small_blind),
            {str(seat): self.stacks[seat] for seat in participants},
            timing="before_hand",
        )
        self.participants = participants
        self.waiting_for_top_up = tuple(sorted(waiting))
        self.hand_number += 1
        self._accept(candidate)
        return self.hand

    def _accept(self, candidate: MushroomHand) -> None:
        was_terminal = self.hand is not None and self.hand.state.terminal
        self.hand = candidate
        balances = list(self.stacks)
        for seat, amount in zip(self.participants, candidate.table_stacks, strict=True):
            balances[seat] = amount
        self.stacks = tuple(balances)
        if candidate.state.terminal and not was_terminal:
            winners = candidate.state.side_pots[0].winners
            sole = len(winners) == 1
            self.ledger.settle_hand(
                f"settle:{self.hand_number}",
                str(self.hand_number),
                str(self.participants[winners[0]]) if sole else None,
                outcome="sole_main_pot" if sole else "split_main_pot",
            )
            self.button = next(
                seat for seat in self._clockwise(self.button) if seat in self.participants
            )
        self.validate()

    def transition(self, action: str) -> None:
        if self.hand is None or self.ended:
            raise ValueError("No open hand")
        self._accept(self.hand.transition(action))

    def deal_to_decision(
        self, *, rng: Random | None = None, deck: Sequence[str] | None = None
    ) -> None:
        if self.hand is None or self.ended:
            raise ValueError("No open hand")
        self._accept(self.hand.deal_to_decision(rng=rng, deck=deck))

    def top_up(self, seat: int, amount: int) -> None:
        if self.ended or (self.hand is not None and not self.hand.state.terminal):
            raise ValueError("Top-ups require an active session between hands")
        if type(seat) is not int or not 0 <= seat < len(self.stacks):
            raise ValueError("Invalid seat")
        if type(amount) is not int or amount <= 0:
            raise ValueError("Top-up must be positive integer points")
        if self.stacks[seat] + amount < 400:
            raise ValueError("Top-up must leave at least 400 points in the player's stack")
        balances = list(self.stacks)
        balances[seat] += amount
        self.stacks = tuple(balances)
        self.funding_total += amount
        self.waiting_for_top_up = tuple(s for s in self.waiting_for_top_up if s != seat)
        self.validate()

    def validate(self) -> None:
        if not self.ledger.snapshot().check_conservation():
            raise ValueError("Invalid mushroom escrow")
        pot = 0
        if self.hand is not None and not self.ended:
            self.hand.validate()
            if not self.hand.state.terminal:
                pot = self.hand.state.pot
            if self.hand.pool != self.pool:
                raise ValueError("Hand and session mushroom pools disagree")
        if sum(self.stacks) + self.pool + pot != self.funding_total:
            raise ValueError("Table plus pots does not conserve total funding")
