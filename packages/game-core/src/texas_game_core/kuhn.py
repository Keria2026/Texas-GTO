"""Exact finite Kuhn poker, including a documented multi-player extension.

Every seat antes one unit and receives one distinct rank from a deck of n + 1
cards. Seats act in order, starting at zero. There is at most one one-unit bet;
after it every other seat responds once, including seats that checked earlier.
Otherwise everybody checks and shows down. The highest remaining rank wins.
For two seats these are the conventional Kuhn rules (value -1/18 for seat zero).
This finite research game is not no-limit Texas hold'em.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import permutations
from typing import ClassVar

from .interfaces import CHANCE_PLAYER


@dataclass(frozen=True, slots=True)
class KuhnState:
    cards: tuple[int, ...] = ()
    history: tuple[str, ...] = ()


@lru_cache(maxsize=5)
def _deals(players: int) -> tuple[tuple[str, float], ...]:
    cards = tuple(permutations(range(players + 1), players))
    probability = 1 / len(cards)
    return tuple(("deal:" + ",".join(map(str, deal)), probability) for deal in cards)


@dataclass(frozen=True, slots=True)
class KuhnPoker:
    num_players: int = 2
    game_id: ClassVar[str] = "kuhn"
    final_betting_round: ClassVar[int] = 0

    def betting_round(self, state: KuhnState) -> int:
        return 0

    def __post_init__(self) -> None:
        if type(self.num_players) is not int or not 2 <= self.num_players <= 6:
            raise ValueError("Kuhn supports two through six players")

    @property
    def rules_version(self) -> str:
        return f"kuhn-{self.num_players}p-v1"

    def initial_state(self) -> KuhnState:
        return KuhnState()

    def _inspect(self, state: KuhnState) -> tuple[int | None, tuple[int, ...], tuple[int, ...]]:
        if not isinstance(state, KuhnState):
            raise ValueError("Expected a KuhnState")
        if not isinstance(state.cards, tuple) or not isinstance(state.history, tuple):
            raise ValueError("Cards and history must be immutable tuples")
        if not state.cards:
            if state.history:
                raise ValueError("Actions cannot precede the deal")
            return CHANCE_PLAYER, (1,) * self.num_players, tuple(range(self.num_players))
        if (
            len(state.cards) != self.num_players
            or any(
                type(card) is not int or not 0 <= card <= self.num_players for card in state.cards
            )
            or len(set(state.cards)) != self.num_players
        ):
            raise ValueError("Private cards must be distinct ranks from the configured deck")
        contributions = [1] * self.num_players
        active = [True] * self.num_players
        actor: int | None = 0
        bet = False
        responses = 0
        checks = 0
        for action in state.history:
            if actor is None:
                raise ValueError("An action follows the terminal history")
            if not isinstance(action, str) or action not in (
                ("fold", "call") if bet else ("check", "bet")
            ):
                raise ValueError("Illegal Kuhn action for this history")
            if not bet:
                if action == "check":
                    checks += 1
                    actor = None if checks == self.num_players else actor + 1
                else:
                    contributions[actor] += 1
                    bet = True
                    actor = (actor + 1) % self.num_players
            else:
                if action == "call":
                    contributions[actor] += 1
                else:
                    active[actor] = False
                responses += 1
                actor = (
                    None if responses == self.num_players - 1 else (actor + 1) % self.num_players
                )
        return actor, tuple(contributions), tuple(i for i, live in enumerate(active) if live)

    def actor(self, state: KuhnState) -> int | None:
        return self._inspect(state)[0]

    def legal_actions(self, state: KuhnState) -> tuple[str, ...]:
        actor = self.actor(state)
        if actor is None:
            return ()
        if actor == CHANCE_PLAYER:
            return tuple(action for action, _ in _deals(self.num_players))
        return ("fold", "call") if "bet" in state.history else ("check", "bet")

    def chance_outcomes(self, state: KuhnState) -> tuple[tuple[str, float], ...]:
        return _deals(self.num_players) if self.actor(state) == CHANCE_PLAYER else ()

    def transition(self, state: KuhnState, action: str) -> KuhnState:
        actor = self.actor(state)
        if actor is None:
            raise ValueError("Cannot act after the hand ends")
        if actor == CHANCE_PLAYER:
            if not isinstance(action, str) or not action.startswith("deal:"):
                raise ValueError("Expected a joint private-card deal")
            try:
                cards = tuple(int(card) for card in action[5:].split(","))
            except ValueError as error:
                raise ValueError("Invalid deal action") from error
            if action != "deal:" + ",".join(map(str, cards)):
                raise ValueError("Deal actions must use canonical integer ranks")
            result = KuhnState(cards=cards)
            self._inspect(result)
            return result
        if action not in self.legal_actions(state):
            raise ValueError("Illegal Kuhn action")
        return KuhnState(state.cards, (*state.history, action))

    def active_players(self, state: KuhnState) -> tuple[int, ...]:
        return self._inspect(state)[2]

    def terminal_payoffs(self, state: KuhnState) -> tuple[float, ...]:
        actor, contributions, active = self._inspect(state)
        if actor is not None:
            raise ValueError("Payoffs are defined only at terminal states")
        winner = max(active, key=lambda player: state.cards[player])
        pot = sum(contributions)
        return tuple(
            float((pot if i == winner else 0) - value) for i, value in enumerate(contributions)
        )

    def public_key(self, state: KuhnState) -> str:
        self._inspect(state)
        phase = "play" if state.cards else "deal"
        return f"{self.rules_version}|{phase}|{','.join(state.history)}"

    def information_key(self, state: KuhnState, player: int) -> str:
        self._validate_player(player)
        public = self.public_key(state)
        rank = str(state.cards[player]) if state.cards else "?"
        return f"{public}|player:{player}|card:{rank}"

    def observation(self, state: KuhnState, player: int) -> dict:
        self._validate_player(player)
        actor, contributions, active = self._inspect(state)
        return {
            "game_id": self.game_id,
            "rules_version": self.rules_version,
            "num_players": self.num_players,
            "player": player,
            "private_card": state.cards[player] if state.cards else None,
            "public_history": list(state.history),
            "actor": actor,
            "contributions": list(contributions),
            "active_players": list(active),
            "legal_actions": list(self.legal_actions(state)) if actor == player else [],
            "terminal": actor is None,
            "payoffs": list(self.terminal_payoffs(state)) if actor is None else None,
        }

    def replay(self, cards: Sequence[int], actions: Sequence[str] = ()) -> KuhnState:
        state = self.transition(self.initial_state(), "deal:" + ",".join(map(str, cards)))
        for action in actions:
            state = self.transition(state, action)
        return state

    def _validate_player(self, player: int) -> None:
        if type(player) is not int or not 0 <= player < self.num_players:
            raise ValueError("Invalid player index")
