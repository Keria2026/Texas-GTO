"""Information boundaries shared by exact and sampled game traversals."""

from collections.abc import Hashable, Sequence
from typing import Protocol

CHANCE_PLAYER = -1


class ExtensiveGame[State, Action: Hashable](Protocol):
    """A finite game with immutable states and player-specific information sets.

    Concrete games must validate actions, chance probabilities and payoff units.
    The information key may contain only observations available to that player.
    A terminal state returns ``None`` from actor; chance returns CHANCE_PLAYER.
    """

    @property
    def num_players(self) -> int: ...

    def initial_state(self) -> State: ...

    def actor(self, state: State) -> int | None: ...

    def legal_actions(self, state: State) -> Sequence[Action]: ...

    def chance_outcomes(self, state: State) -> Sequence[tuple[Action, float]]: ...

    def information_key(self, state: State, player: int) -> Hashable: ...

    def transition(self, state: State, action: Action) -> State: ...

    def terminal_payoffs(self, state: State) -> tuple[float, ...]: ...
