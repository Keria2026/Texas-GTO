"""可重放的标准无限注德州模拟器；整数筹码、无抽水、单次发牌。

The simulator exposes exact card information only to its owner. Betting actions
use ``raise_to:N`` (total street commitment), never an ambiguous raise increment.
``legal_actions`` is a finite betting abstraction for research; ``transition``
also accepts every otherwise legal integer raise. This is a rules engine, not a
trained poker strategy or a reproduction of the Pluribus result.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, replace

from texas_game_core.cards import DECK, evaluate_best, normalize_card, validate_cards
from texas_game_core.interfaces import CHANCE_PLAYER

STREETS = ("preflop", "flop", "turn", "river")


@dataclass(frozen=True)
class HoldemAction:
    street: str
    player: int
    action: str
    amount_delta: int
    amount_to: int
    in_action_abstraction: bool = True


@dataclass(frozen=True)
class SidePot:
    amount: int
    contributors: tuple[int, ...]
    eligible: tuple[int, ...]
    winners: tuple[int, ...]
    awards: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class HoldemState:
    rules_version: str
    stacks: tuple[int, ...]
    contributions: tuple[int, ...]
    street_bets: tuple[int, ...]
    hole_cards: tuple[tuple[str, ...], ...]
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
    terminal: bool = False
    payouts: tuple[int, ...] = ()
    returned: tuple[int, ...] = ()
    side_pots: tuple[SidePot, ...] = ()

    @property
    def street_name(self) -> str:
        return STREETS[self.street]

    @property
    def pot(self) -> int:
        return 0 if self.terminal else sum(self.contributions)


class HoldemGame:
    """2–6 seats, fixed starting stacks/button and a finite action abstraction.

    There are no antes, rake, straddles, burns or mushroom rewards. Omitting burn
    cards preserves the distribution of visible cards in a shuffled deck. A
    short big blind does not lower the preflop bring-in while two funded players
    can still contest it. Odd chips go clockwise from the seat left of the
    button. The rule version records all strategy-relevant configuration.
    """

    game_id = "holdem"
    supports_exact_enumeration = False

    def __init__(
        self,
        stacks: Sequence[int] = (40, 40, 40, 40, 40, 40),
        *,
        small_blind: int = 1,
        big_blind: int = 2,
        button: int = 0,
        bet_fractions: Sequence[float] = (0.5, 1.0),
    ) -> None:
        self.initial_stacks = tuple(stacks)
        if not 2 <= len(self.initial_stacks) <= 6:
            raise ValueError("Hold'em requires 2 to 6 players")
        if any(type(value) is not int or value <= 0 for value in self.initial_stacks):
            raise ValueError("starting stacks must be positive integer chips")
        if (
            type(small_blind) is not int
            or type(big_blind) is not int
            or not 0 < small_blind <= big_blind
        ):
            raise ValueError("blinds must be positive integers with small_blind <= big_blind")
        if type(button) is not int or not 0 <= button < self.num_players:
            raise ValueError("button is outside the table")
        fractions = tuple(float(value) for value in bet_fractions)
        if any(not math.isfinite(value) or value <= 0 for value in fractions):
            raise ValueError("bet fractions must be positive and finite")
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.button = button
        self.bet_fractions = tuple(sorted(set(fractions)))
        self.rules_version = "holdem-nl-v1:" + json.dumps(
            {
                "stacks": self.initial_stacks,
                "smallBlind": small_blind,
                "bigBlind": big_blind,
                "button": button,
                "betFractions": self.bet_fractions,
                "rake": 0,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def num_players(self) -> int:
        return len(self.initial_stacks)

    @property
    def small_blind_player(self) -> int:
        return self.button if self.num_players == 2 else (self.button + 1) % self.num_players

    @property
    def big_blind_player(self) -> int:
        return (self.small_blind_player + 1) % self.num_players

    def _clockwise(self, start: int) -> tuple[int, ...]:
        return tuple((start + offset) % self.num_players for offset in range(self.num_players))

    def initial_state(self) -> HoldemState:
        stacks = list(self.initial_stacks)
        bets = [0] * self.num_players
        for player, blind in (
            (self.small_blind_player, self.small_blind),
            (self.big_blind_player, self.big_blind),
        ):
            bets[player] = min(blind, stacks[player])
            stacks[player] -= bets[player]
        # Two rounds clockwise from left of button, with private single-card
        # chance events. No enormous combination-valued chance outcome list.
        deal_order = self._clockwise((self.button + 1) % self.num_players)
        first = (
            self.small_blind_player
            if self.num_players == 2
            else (self.big_blind_player + 1) % self.num_players
        )
        return HoldemState(
            rules_version=self.rules_version,
            stacks=tuple(stacks),
            contributions=tuple(bets),
            street_bets=tuple(bets),
            hole_cards=((),) * self.num_players,
            board=(),
            folded=(False,) * self.num_players,
            street=0,
            current_bet=self.big_blind,
            last_full_raise=self.big_blind,
            acted_to=(None,) * self.num_players,
            pending=self._clockwise(first),
            pending_deal=deal_order * 2,
            to_act=None,
            returned=(0,) * self.num_players,
        )

    def _check_game(self, state: HoldemState) -> None:
        if state.rules_version != self.rules_version:
            raise ValueError("state belongs to a different rule configuration")

    def validate_state(self, state: HoldemState) -> None:
        """Validate a deserialized/reconstructed state before using its actions.

        Reachability still requires replaying its complete events; these checks
        reject structural/card/accounting contradictions rather than certifying
        an arbitrary caller-created history as an observed hand.
        """
        self._check_game(state)
        vectors = (
            state.stacks,
            state.contributions,
            state.street_bets,
            state.hole_cards,
            state.folded,
            state.acted_to,
            state.returned,
        )
        if any(len(vector) != self.num_players for vector in vectors):
            raise ValueError("state vectors must match the number of players")
        for amounts in (state.stacks, state.contributions, state.street_bets, state.returned):
            if any(type(amount) is not int or amount < 0 for amount in amounts):
                raise ValueError("state amounts must be nonnegative integer chips")
        if any(type(folded) is not bool for folded in state.folded):
            raise ValueError("fold status must be boolean")
        if not any(not folded for folded in state.folded):
            raise ValueError("at least one player must remain active")
        if any(len(hand) > 2 for hand in state.hole_cards):
            raise ValueError("a player cannot have more than two hole cards")
        cards = tuple(card for hand in state.hole_cards for card in hand) + state.board
        if validate_cards(cards) != cards:
            raise ValueError("state cards must use canonical spelling")
        if type(state.street) is not int or not 0 <= state.street <= 3:
            raise ValueError("unknown betting street")
        expected_board = (0, 3, 4, 5)[state.street]
        board_deals = state.pending_deal.count(-1)
        if len(state.board) + board_deals != expected_board:
            raise ValueError("board and pending deals do not match the street")
        if state.street > 0 and any(len(hand) != 2 for hand in state.hole_cards):
            raise ValueError("all private cards must be dealt before the flop")
        if state.street == 0:
            if board_deals or sum(map(len, state.hole_cards)) + len(state.pending_deal) != (
                2 * self.num_players
            ):
                raise ValueError("private deal count is inconsistent")
        elif board_deals != len(state.pending_deal):
            raise ValueError("private cards cannot be dealt after preflop")
        if (
            type(state.current_bet) is not int
            or state.current_bet < max(state.street_bets)
            or type(state.last_full_raise) is not int
            or state.last_full_raise < self.big_blind
        ):
            raise ValueError("invalid current wager or full raise increment")
        if any(
            amount is not None and (type(amount) is not int or amount < 0)
            for amount in state.acted_to
        ):
            raise ValueError("invalid acted-to amount")
        if len(state.pending) != len(set(state.pending)) or any(
            type(player) is not int or not 0 <= player < self.num_players
            for player in state.pending
        ):
            raise ValueError("invalid pending player order")
        if any(
            type(player) is not int or not -1 <= player < self.num_players
            for player in state.pending_deal
        ):
            raise ValueError("invalid card recipient")
        if state.terminal:
            if state.pending or state.pending_deal or state.to_act is not None:
                raise ValueError("a terminal state cannot have pending actions")
            if len(state.payouts) != self.num_players or any(
                type(value) is not int or value < 0 for value in state.payouts
            ):
                raise ValueError("invalid terminal payouts")
        elif state.payouts:
            raise ValueError("payouts are available only after settlement")
        elif state.pending_deal:
            if state.to_act is not None:
                raise ValueError("players cannot act during a chance event")
        elif (
            not state.pending
            or state.to_act != state.pending[0]
            or state.folded[state.to_act]
            or state.stacks[state.to_act] <= 0
        ):
            raise ValueError("invalid acting player")
        for player in range(self.num_players):
            if state.street_bets[player] > state.contributions[player]:
                raise ValueError("street commitment exceeds the hand commitment")
            payout = state.payouts[player] if state.terminal else 0
            if state.stacks[player] + state.contributions[player] != (
                self.initial_stacks[player] + payout
            ):
                raise ValueError("player chip accounting is inconsistent")
        total = sum(state.stacks) + (0 if state.terminal else sum(state.contributions))
        if total != sum(self.initial_stacks):
            raise ValueError("chip conservation failed")

    def active_players(self, state: HoldemState) -> tuple[int, ...]:
        self._check_game(state)
        return tuple(player for player in range(self.num_players) if not state.folded[player])

    def actor(self, state: HoldemState) -> int | None:
        self._check_game(state)
        if state.terminal:
            return None
        if state.pending_deal:
            return CHANCE_PLAYER
        return state.to_act

    def _remaining_cards(self, state: HoldemState) -> tuple[str, ...]:
        used = set(state.board)
        used.update(card for hand in state.hole_cards for card in hand)
        return tuple(card for card in DECK if card not in used)

    def chance_outcomes(self, state: HoldemState) -> tuple[tuple[str, float], ...]:
        if self.actor(state) != CHANCE_PLAYER:
            return ()
        cards = self._remaining_cards(state)
        return tuple((f"deal:{card}", 1.0 / len(cards)) for card in cards)

    def sample_chance(self, state: HoldemState, rng: random.Random) -> str:
        """Draw one exact chance action without constructing probability pairs."""
        return self.sample_chance_outcome(state, rng)[0]

    def sample_chance_outcome(self, state: HoldemState, rng: random.Random) -> tuple[str, float]:
        """Draw one action and its true probability for sampled CFR averaging."""
        if self.actor(state) != CHANCE_PLAYER:
            raise ValueError("state is not a chance node")
        remaining = self._remaining_cards(state)
        return f"deal:{rng.choice(remaining)}", 1.0 / len(remaining)

    def amount_to_call(self, state: HoldemState, player: int | None = None) -> int:
        self._check_game(state)
        seat = state.to_act if player is None else player
        if seat is None or not 0 <= seat < self.num_players:
            raise ValueError("no valid acting player")
        return min(state.stacks[seat], max(0, state.current_bet - state.street_bets[seat]))

    def minimum_raise_to(self, state: HoldemState) -> int:
        # No-limit hold'em has no stud-style completion. Even after a short
        # opening all-in, a non-all-in raise adds the full minimum increment.
        return state.current_bet + state.last_full_raise

    def _can_raise(self, state: HoldemState, player: int) -> bool:
        if state.street_bets[player] + state.stacks[player] <= state.current_bet:
            return False
        if not any(
            other != player and not state.folded[other] and state.stacks[other] > 0
            for other in range(self.num_players)
        ):
            return False
        acted = state.acted_to[player]
        return acted is None or state.current_bet - acted >= state.last_full_raise

    def legal_actions(self, state: HoldemState) -> tuple[str, ...]:
        actor = self.actor(state)
        if actor is None:
            return ()
        if actor == CHANCE_PLAYER:
            return tuple(action for action, _ in self.chance_outcomes(state))
        call = self.amount_to_call(state)
        actions = ["fold", "call"] if call else ["check"]
        if self._can_raise(state, actor):
            maximum = state.street_bets[actor] + state.stacks[actor]
            minimum = self.minimum_raise_to(state)
            candidates = {maximum}
            if minimum <= maximum:
                candidates.add(minimum)
                # A pot-sized raise increases the current wager by the pot
                # after calling; the result is a total street commitment.
                pot_after_call = state.pot + call
                for fraction in self.bet_fractions:
                    numerator, denominator = fraction.as_integer_ratio()
                    target = state.current_bet + numerator * pot_after_call // denominator
                    candidates.add(min(maximum, max(minimum, target)))
            actions.extend(f"raise_to:{target}" for target in sorted(candidates))
        return tuple(actions)

    def transition(self, state: HoldemState, action: str) -> HoldemState:
        self.validate_state(state)
        actor = self.actor(state)
        if actor is None:
            raise ValueError("cannot act on a terminal state")
        if not isinstance(action, str):
            raise ValueError("action must be a string")
        if actor == CHANCE_PLAYER:
            return self._deal(state, action)
        stacks = list(state.stacks)
        committed = list(state.contributions)
        bets = list(state.street_bets)
        folded = list(state.folded)
        acted_to = list(state.acted_to)
        current_bet = state.current_bet
        last_full_raise = state.last_full_raise
        pending = tuple(player for player in state.pending if player != actor)
        call = self.amount_to_call(state)
        amount = 0
        if action == "fold":
            if not call:
                raise ValueError("check is available; folding without a wager is not exposed")
            folded[actor] = True
        elif action == "check":
            if call:
                raise ValueError("cannot check facing a wager")
            # Checking before an opening wager preserves the right to raise,
            # including when the first wager is an incomplete all-in.
            acted_to[actor] = current_bet if current_bet else None
        elif action == "call":
            if not call:
                raise ValueError("nothing to call")
            amount = call
            acted_to[actor] = current_bet
        elif action.startswith("raise_to:"):
            raw_target = action.removeprefix("raise_to:")
            if not raw_target.isascii() or not raw_target.isdecimal():
                raise ValueError("raise_to requires an integer chip amount")
            target = int(raw_target)
            if action != f"raise_to:{target}":
                raise ValueError("raise_to amount must use canonical integer formatting")
            if not self._can_raise(state, actor):
                raise ValueError("raising is unavailable or has not been reopened")
            maximum = bets[actor] + stacks[actor]
            if not current_bet < target <= maximum:
                raise ValueError("raise_to must exceed the wager without exceeding the stack")
            minimum = self.minimum_raise_to(state)
            if target < minimum and target != maximum:
                raise ValueError("an incomplete raise is only legal when all-in")
            if target >= minimum:
                last_full_raise = target - current_bet
            amount = target - bets[actor]
            current_bet = target
            acted_to[actor] = target
            pending = tuple(
                player
                for player in self._clockwise((actor + 1) % self.num_players)
                if player != actor and not folded[player] and stacks[player] > 0
            )
        else:
            raise ValueError(f"unknown action: {action}")
        stacks[actor] -= amount
        committed[actor] += amount
        bets[actor] += amount
        result = replace(
            state,
            stacks=tuple(stacks),
            contributions=tuple(committed),
            street_bets=tuple(bets),
            folded=tuple(folded),
            current_bet=current_bet,
            last_full_raise=last_full_raise,
            acted_to=tuple(acted_to),
            pending=pending,
            to_act=None,
            history=state.history
            + (
                HoldemAction(
                    state.street_name,
                    actor,
                    action,
                    amount,
                    bets[actor],
                    action in self.legal_actions(state),
                ),
            ),
        )
        return self._continue(result)

    def _deal(self, state: HoldemState, action: str) -> HoldemState:
        if not action.startswith("deal:"):
            raise ValueError("a card chance action is required")
        card = normalize_card(action.removeprefix("deal:"))
        if action != f"deal:{card}" or card not in self._remaining_cards(state):
            raise ValueError("card must be canonical and must not already be dealt")
        recipient = state.pending_deal[0]
        hands = list(state.hole_cards)
        board = state.board
        if recipient == -1:
            board += (card,)
        else:
            hands[recipient] += (card,)
        result = replace(
            state,
            hole_cards=tuple(hands),
            board=board,
            pending_deal=state.pending_deal[1:],
        )
        return result if result.pending_deal else self._continue(result)

    def _continue(self, state: HoldemState) -> HoldemState:
        active = self.active_players(state)
        if len(active) == 1:
            return self._settle(state)
        funded = tuple(player for player in active if state.stacks[player] > 0)
        pending = tuple(player for player in state.pending if player in funded)
        if len(funded) <= 1:
            # The nominal bring-in of a short big blind applies only while
            # another funded opponent can contest the amount above that blind.
            state = replace(state, current_bet=max(state.street_bets[player] for player in active))
            # A lone funded player acts only if another player's actual wager
            # still needs calling. There is no side pot to bet into alone.
            if not funded or state.street_bets[funded[0]] >= max(
                state.street_bets[player] for player in active if player != funded[0]
            ):
                pending = ()
        if pending:
            return replace(state, pending=pending, to_act=pending[0])
        state = self._return_uncalled(state)
        if state.street == 3:
            return self._settle(state)
        street = state.street + 1
        return replace(
            state,
            street=street,
            street_bets=(0,) * self.num_players,
            current_bet=0,
            last_full_raise=self.big_blind,
            acted_to=(None,) * self.num_players,
            pending=self._clockwise((self.button + 1) % self.num_players),
            pending_deal=(-1,) * (3 if street == 1 else 1),
            to_act=None,
        )

    def _return_uncalled(self, state: HoldemState) -> HoldemState:
        ordered = sorted(state.street_bets, reverse=True)
        excess = ordered[0] - ordered[1]
        if excess <= 0:
            return state
        player = state.street_bets.index(ordered[0])
        stacks = list(state.stacks)
        committed = list(state.contributions)
        bets = list(state.street_bets)
        returned = list(state.returned)
        stacks[player] += excess
        committed[player] -= excess
        bets[player] -= excess
        returned[player] += excess
        return replace(
            state,
            stacks=tuple(stacks),
            contributions=tuple(committed),
            street_bets=tuple(bets),
            returned=tuple(returned),
            current_bet=max(bets),
        )

    def _settle(self, state: HoldemState) -> HoldemState:
        state = self._return_uncalled(state)
        active = self.active_players(state)
        if len(active) > 1 and len(state.board) != 5:
            raise ValueError("showdown requires five board cards")
        ranks = (
            {player: evaluate_best(state.hole_cards[player] + state.board) for player in active}
            if len(active) > 1
            else {}
        )
        payouts = [0] * self.num_players
        returned = list(state.returned)
        pots: list[SidePot] = []
        previous = 0
        for level in sorted(set(state.contributions) - {0}):
            contributors = tuple(
                player for player, amount in enumerate(state.contributions) if amount >= level
            )
            amount = (level - previous) * len(contributors)
            previous = level
            if len(contributors) == 1:
                # This also handles a previous-street imbalance in a hand
                # reconstructed from a valid all-in sequence.
                payouts[contributors[0]] += amount
                returned[contributors[0]] += amount
                continue
            eligible = tuple(player for player in contributors if not state.folded[player])
            if not eligible:
                raise ValueError("a pot cannot have no eligible player")
            best = max((ranks[player] for player in eligible), default=()) if ranks else ()
            tied = set(player for player in eligible if not ranks or ranks[player] == best)
            winners = tuple(
                player
                for player in self._clockwise((self.button + 1) % self.num_players)
                if player in tied
            )
            share, odd = divmod(amount, len(winners))
            awards = tuple((player, share + (index < odd)) for index, player in enumerate(winners))
            for player, award in awards:
                payouts[player] += award
            pots.append(SidePot(amount, contributors, eligible, winners, awards))
        final_stacks = tuple(
            stack + payout for stack, payout in zip(state.stacks, payouts, strict=True)
        )
        if sum(final_stacks) != sum(self.initial_stacks):
            raise ValueError("chip conservation failed")
        return replace(
            state,
            stacks=final_stacks,
            terminal=True,
            pending=(),
            pending_deal=(),
            to_act=None,
            payouts=tuple(payouts),
            returned=tuple(returned),
            side_pots=tuple(pots),
        )

    def terminal_payoffs(self, state: HoldemState) -> tuple[float, ...]:
        self._check_game(state)
        if not state.terminal:
            raise ValueError("payoffs are available only after settlement")
        return tuple(
            float(end - start) for end, start in zip(state.stacks, self.initial_stacks, strict=True)
        )

    def public_key(self, state: HoldemState) -> str:
        self._check_game(state)
        # Private chance outcomes and all unknown hole cards are deliberately
        # absent. Deal counts are public, as is which street is being dealt.
        return json.dumps(
            {
                "rules": self.rules_version,
                "street": state.street,
                "board": state.board,
                "stacks": state.stacks,
                "bets": state.street_bets,
                "contributions": state.contributions,
                "folded": state.folded,
                "currentBet": state.current_bet,
                "lastFullRaise": state.last_full_raise,
                "actedTo": state.acted_to,
                "pending": state.pending,
                "dealCount": len(state.pending_deal),
                "toAct": state.to_act,
                "terminal": state.terminal,
                "history": tuple(
                    (item.street, item.player, item.action, item.amount_delta, item.amount_to)
                    for item in state.history
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def information_key(self, state: HoldemState, player: int) -> str:
        if type(player) is not int or not 0 <= player < self.num_players:
            raise ValueError("player is outside the table")
        return json.dumps(
            (player, tuple(sorted(state.hole_cards[player])), self.public_key(state)),
            separators=(",", ":"),
        )

    def observation(self, state: HoldemState, player: int) -> dict[str, object]:
        self.information_key(state, player)  # rule and seat validation
        actor = self.actor(state)
        can_raise = actor is not None and actor != CHANCE_PLAYER and self._can_raise(state, actor)
        return {
            "source": "simulation",
            "gameId": self.game_id,
            "rulesVersion": self.rules_version,
            "hero": player,
            "heroCards": list(state.hole_cards[player]),
            "board": list(state.board),
            "street": state.street_name,
            "button": self.button,
            "smallBlindPlayer": self.small_blind_player,
            "bigBlindPlayer": self.big_blind_player,
            "pot": state.pot,
            "currentBet": state.current_bet,
            "toAct": actor,
            "terminal": state.terminal,
            "legalActions": list(self.legal_actions(state)) if actor == player else [],
            "actionAbstraction": {
                "kind": "finite_bet_sizes",
                "betFractions": list(self.bet_fractions),
                "includesMinimumRaise": True,
                "includesAllIn": True,
                "otherLegalIntegerRaisesAccepted": True,
                "offTreeActionCount": sum(not item.in_action_abstraction for item in state.history),
            },
            "betting": {
                "amountToCall": self.amount_to_call(state)
                if actor is not None and actor >= 0
                else 0,
                "canRaise": can_raise,
                "minimumRaiseTo": self.minimum_raise_to(state) if can_raise else None,
                "maximumRaiseTo": (
                    state.street_bets[actor] + state.stacks[actor] if can_raise else None
                ),
                "shortAllInOnly": (
                    state.street_bets[actor] + state.stacks[actor] < self.minimum_raise_to(state)
                    if can_raise
                    else False
                ),
            },
            "players": [
                {
                    "seat": seat,
                    "stack": state.stacks[seat],
                    "committed": state.contributions[seat],
                    "streetBet": state.street_bets[seat],
                    "folded": state.folded[seat],
                    "allIn": state.stacks[seat] == 0 and not state.folded[seat],
                    "cards": list(state.hole_cards[seat]) if seat == player else None,
                }
                for seat in range(self.num_players)
            ],
            "history": [
                {
                    "street": item.street,
                    "player": item.player,
                    "action": item.action,
                    "amountDelta": item.amount_delta,
                    "amountTo": item.amount_to,
                    "inActionAbstraction": item.in_action_abstraction,
                }
                for item in state.history
            ],
            "payouts": list(state.payouts),
            "returned": list(state.returned),
            "sidePots": [
                {
                    "amount": pot.amount,
                    "eligible": list(pot.eligible),
                    "winners": list(pot.winners),
                    "awards": dict(pot.awards),
                }
                for pot in state.side_pots
            ],
            "payoffs": list(self.terminal_payoffs(state)) if state.terminal else None,
        }

    def deal_to_decision(
        self,
        state: HoldemState,
        *,
        rng: random.Random | None = None,
        deck: Sequence[str] | None = None,
    ) -> HoldemState:
        """Consume chance events; an explicit deck may be a sufficient prefix.

        Reuse an RNG across streets for deterministic sampling. Explicit decks
        are ordered from the first card dealt, and cannot repeat a card.
        """
        rng = rng if rng is not None else random.Random(0)
        ordered_deck = validate_cards(deck) if deck is not None else None
        while self.actor(state) == CHANCE_PLAYER:
            remaining = self._remaining_cards(state)
            if ordered_deck is None:
                card = rng.choice(remaining)
            else:
                card = next((card for card in ordered_deck if card in remaining), None)
                if card is None:
                    raise ValueError("explicit deck exhausted before the next decision")
            state = self.transition(state, f"deal:{card}")
        return state

    def replay(
        self,
        actions: Sequence[str],
        *,
        seed: int = 0,
        deck: Sequence[str] | None = None,
    ) -> HoldemState:
        """Replay player actions with seeded or explicitly ordered chance cards."""
        rng = random.Random(seed)
        state = self.deal_to_decision(self.initial_state(), rng=rng, deck=deck)
        for action in actions:
            state = self.transition(state, action)
            state = self.deal_to_decision(state, rng=rng, deck=deck)
        return state
