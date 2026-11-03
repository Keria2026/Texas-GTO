"""Exact two-player Leduc research variant with explicit betting conventions.

Six physical cards contain two copies each of ranks 0, 1 and 2. Each player
antes one, receives one card, and bets before and after one shared board card.
Bet increments are two then four. Each round permits at most two increases
(the opening bet and one raise), and seat zero acts first in both rounds.
A pair with the board beats a high card; equal ranks split the pot. Physical
card IDs remain hidden in information keys, which expose ranks only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import permutations
from typing import ClassVar

from .interfaces import CHANCE_PLAYER

_DEALS = tuple(("deal:" + ",".join(map(str, cards)), 1 / 30) for cards in permutations(range(6), 2))


@dataclass(frozen=True, slots=True)
class LeducState:
    cards: tuple[int, ...] = ()
    board: int | None = None
    rounds: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())


def _round_position(
    history: tuple[str, ...], bet_size: int
) -> tuple[int | None, tuple[int, int], int | None, int]:
    actor: int | None = 0
    paid = [0, 0]
    increases = 0
    checks = 0
    folded: int | None = None
    for action in history:
        if actor is None:
            raise ValueError("An action follows a completed betting round")
        facing = max(paid) - paid[actor]
        legal = (
            ("check", "bet")
            if not facing
            else (("fold", "call", "raise") if increases < 2 else ("fold", "call"))
        )
        if not isinstance(action, str) or action not in legal:
            raise ValueError("Illegal Leduc action for this history")
        if action == "fold":
            folded = actor
            actor = None
        elif action == "call":
            paid[actor] = max(paid)
            actor = None
        elif action == "check":
            checks += 1
            actor = None if checks == 2 else 1 - actor
        else:
            paid[actor] = max(paid) + bet_size
            increases += 1
            actor = 1 - actor
    return actor, (paid[0], paid[1]), folded, increases


@dataclass(frozen=True, slots=True)
class LeducPoker:
    num_players: int = 2
    game_id: ClassVar[str] = "leduc"
    rules_version: ClassVar[str] = "leduc-2p-v1"
    final_betting_round: ClassVar[int] = 1

    def betting_round(self, state: LeducState) -> int:
        return int(state.board is not None)

    def __post_init__(self) -> None:
        if type(self.num_players) is not int or self.num_players != 2:
            raise ValueError("This Leduc variant supports exactly two players")

    def initial_state(self) -> LeducState:
        return LeducState()

    def _inspect(
        self, state: LeducState
    ) -> tuple[int | None, tuple[int, ...], tuple[int, ...], int, int]:
        if not isinstance(state, LeducState):
            raise ValueError("Expected a LeducState")
        if (
            not isinstance(state.cards, tuple)
            or not isinstance(state.rounds, tuple)
            or len(state.rounds) != 2
            or any(not isinstance(history, tuple) for history in state.rounds)
        ):
            raise ValueError("Cards and two round histories must be immutable tuples")
        if not state.cards:
            if state.board is not None or any(state.rounds):
                raise ValueError("Board and actions cannot precede the private deal")
            return CHANCE_PLAYER, (1, 1), (0, 1), 0, 0
        if (
            len(state.cards) != 2
            or any(type(card) is not int or not 0 <= card < 6 for card in state.cards)
            or len(set(state.cards)) != 2
        ):
            raise ValueError("Private cards must be two distinct physical cards")
        actor, first_paid, folded, increases = _round_position(state.rounds[0], 2)
        contributions = tuple(1 + paid for paid in first_paid)
        if folded is not None or actor is not None:
            if state.board is not None or state.rounds[1]:
                raise ValueError("Future street information exists before the first round ends")
            active = tuple(i for i in range(2) if i != folded)
            return actor, contributions, active, 0, increases
        if state.board is None:
            if state.rounds[1]:
                raise ValueError("Second-round actions cannot precede the public card")
            return CHANCE_PLAYER, contributions, (0, 1), 1, 0
        if type(state.board) is not int or not 0 <= state.board < 6 or state.board in state.cards:
            raise ValueError("The public card must be an undealt physical card")
        actor, second_paid, folded, increases = _round_position(state.rounds[1], 4)
        contributions = tuple(contributions[i] + second_paid[i] for i in range(2))
        active = tuple(i for i in range(2) if i != folded)
        return actor, contributions, active, 1, increases

    def actor(self, state: LeducState) -> int | None:
        return self._inspect(state)[0]

    def legal_actions(self, state: LeducState) -> tuple[str, ...]:
        actor, _, _, round_index, increases = self._inspect(state)
        if actor is None:
            return ()
        if actor == CHANCE_PLAYER:
            return tuple(action for action, _ in self.chance_outcomes(state))
        _, paid, _, _ = _round_position(state.rounds[round_index], 2 if round_index == 0 else 4)
        if max(paid) == paid[actor]:
            return ("check", "bet")
        return ("fold", "call", "raise") if increases < 2 else ("fold", "call")

    def chance_outcomes(self, state: LeducState) -> tuple[tuple[str, float], ...]:
        if self.actor(state) != CHANCE_PLAYER:
            return ()
        if not state.cards:
            return _DEALS
        return tuple((f"board:{card}", 0.25) for card in range(6) if card not in state.cards)

    def transition(self, state: LeducState, action: str) -> LeducState:
        actor, _, _, round_index, _ = self._inspect(state)
        if actor is None:
            raise ValueError("Cannot act after the hand ends")
        if action not in self.legal_actions(state):
            raise ValueError("Illegal Leduc action")
        if actor == CHANCE_PLAYER:
            if not state.cards:
                return LeducState(cards=tuple(int(card) for card in action[5:].split(",")))
            return LeducState(state.cards, int(action[6:]), state.rounds)
        history = (*state.rounds[round_index], action)
        rounds = (history, state.rounds[1]) if round_index == 0 else (state.rounds[0], history)
        return LeducState(state.cards, state.board, rounds)

    def active_players(self, state: LeducState) -> tuple[int, ...]:
        return self._inspect(state)[2]

    def terminal_payoffs(self, state: LeducState) -> tuple[float, ...]:
        actor, paid, active, _, _ = self._inspect(state)
        if actor is not None:
            raise ValueError("Payoffs are defined only at terminal states")
        if len(active) == 1:
            winners = active
        else:
            assert state.board is not None
            board_rank = state.board // 2
            strengths = tuple((int(card // 2 == board_rank), card // 2) for card in state.cards)
            strongest = max(strengths)
            winners = tuple(
                player for player, strength in enumerate(strengths) if strength == strongest
            )
        share = sum(paid) / len(winners)
        return tuple((share if player in winners else 0.0) - paid[player] for player in range(2))

    def public_key(self, state: LeducState) -> str:
        self._inspect(state)
        phase = "play" if state.cards else "deal"
        board = str(state.board // 2) if state.board is not None else "?"
        return (
            f"{self.rules_version}|{phase}|pre:{','.join(state.rounds[0])}"
            f"|board:{board}|post:{','.join(state.rounds[1])}"
        )

    def information_key(self, state: LeducState, player: int) -> str:
        self._validate_player(player)
        public = self.public_key(state)
        card = str(state.cards[player] // 2) if state.cards else "?"
        return f"{public}|player:{player}|card:{card}"

    def observation(self, state: LeducState, player: int) -> dict:
        self._validate_player(player)
        actor, paid, active, round_index, _ = self._inspect(state)
        return {
            "game_id": self.game_id,
            "rules_version": self.rules_version,
            "num_players": self.num_players,
            "player": player,
            "private_card": state.cards[player] // 2 if state.cards else None,
            "public_card": state.board // 2 if state.board is not None else None,
            "public_history": [
                *state.rounds[0],
                *([f"board_rank:{state.board // 2}"] if state.board is not None else []),
                *state.rounds[1],
            ],
            "rounds": [list(history) for history in state.rounds],
            "round_index": round_index,
            "actor": actor,
            "contributions": list(paid),
            "active_players": list(active),
            "legal_actions": list(self.legal_actions(state)) if actor == player else [],
            "terminal": actor is None,
            "payoffs": list(self.terminal_payoffs(state)) if actor is None else None,
        }

    def replay(self, cards: Sequence[int], actions: Sequence[str] = ()) -> LeducState:
        state = self.transition(self.initial_state(), "deal:" + ",".join(map(str, cards)))
        for action in actions:
            state = self.transition(state, action)
        return state

    @staticmethod
    def _validate_player(player: int) -> None:
        if type(player) is not int or not 0 <= player < 2:
            raise ValueError("Invalid player index")
