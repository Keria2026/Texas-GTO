"""Finite-horizon mushroom session for counterfactual training and evaluation.

The horizon is an explicit training approximation: after the final completed
hand the remaining pool is refunded by contribution. Live sessions need not end
at that point. This model is not automatically a verified live strategy.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from random import Random

from texas_game_core.mushroom_table import MushroomTable


def mushroom_public_key(
    *,
    rules_version,
    stacks,
    pool,
    outstanding,
    button,
    participants,
    waiting_for_top_up,
    hand_number,
    ended,
    history,
    hand,
):
    """Shared public encoding; no private cards or histories enter this key."""
    return json.dumps(
        {
            "rules": rules_version,
            "stacks": stacks,
            "pool": pool,
            "poolContributions": outstanding,
            "button": button,
            "participants": participants,
            "waitingForTopUp": waiting_for_top_up,
            "handNumber": hand_number,
            "ended": ended,
            "history": history,
            "hand": hand,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def mushroom_information_key(player, private_history, cards, public_key):
    """Encode one observer's known private history without constructing other hands."""
    return json.dumps(
        (player, private_history, tuple(sorted(cards)), sha256(public_key.encode()).hexdigest()),
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class MushroomGameState:
    rules_version: str
    table: MushroomTable
    public_history: tuple[str, ...]
    private_history: tuple[tuple[tuple[str, ...], ...], ...]


class MushroomSessionGame:
    game_id = "mushroom"
    supports_exact_enumeration = False

    def __init__(
        self,
        stacks: Sequence[int] = (800,) * 8,
        *,
        button: int = 0,
        horizon: int = 2,
        outstanding: Sequence[int] | None = None,
        waiting_for_top_up: Sequence[int] = (),
    ):
        """Plan from known pre-fee balances, retaining already-paid refund ownership."""
        self.initial_stacks = tuple(stacks)
        opening = MushroomTable(
            self.initial_stacks,
            button=button,
            outstanding=outstanding,
            waiting_for_top_up=waiting_for_top_up,
        )
        if type(horizon) is not int or not 1 <= horizon <= 16:
            raise ValueError("Training horizon must be 1 to 16 completed hands")
        self.num_players = len(self.initial_stacks)
        contributions = dict(opening.ledger.snapshot().outstanding_contributions)
        self.initial_outstanding = tuple(
            contributions.get(str(seat), 0) for seat in range(self.num_players)
        )
        self.initial_waiting_for_top_up = opening.waiting_for_top_up
        self.initial_wealth = tuple(
            stack + paid
            for stack, paid in zip(self.initial_stacks, self.initial_outstanding, strict=True)
        )
        self.funding_total = opening.funding_total
        self.button = button
        self.horizon = horizon
        self.final_betting_round = 4 * horizon - 1
        opening_rules = {}
        if any(self.initial_outstanding):
            opening_rules["openingOutstanding"] = self.initial_outstanding
        if self.initial_waiting_for_top_up:
            opening_rules["openingWaiting"] = self.initial_waiting_for_top_up
        self.rules_version = "mushroom-session-horizon-v2:" + json.dumps(
            {
                "stacks": self.initial_stacks,
                "button": button,
                "horizon": horizon,
                "smallBlind": 2,
                "bigBlind": 4,
                "straddle": 8,
                "fees": [20, 40],
                "horizonSettlement": "refund_outstanding_contributions",
                "restraddle": "not_supported",
                "shortStraddle": "wait_for_top_up_and_advance_blinds",
                "topUpMinimumResultingStack": 400,
                "waitingPlayers": "excluded_until_explicit_top_up",
                "trainingExternalFunding": "none",
                "informationEncoding": "sha256-public-recall-v1",
                **opening_rules,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def initial_state(self) -> MushroomGameState:
        table = MushroomTable(
            self.initial_stacks,
            button=self.button,
            outstanding=self.initial_outstanding,
            waiting_for_top_up=self.initial_waiting_for_top_up,
        )
        table.begin_hand()
        return MushroomGameState(self.rules_version, table, (), ((),) * self.num_players)

    def _check(self, state: MushroomGameState) -> None:
        if state.rules_version != self.rules_version:
            raise ValueError("State belongs to another mushroom training configuration")
        if state.table.funding_total != self.funding_total:
            raise ValueError("Training state cannot introduce external funding")

    def actor(self, state: MushroomGameState) -> int | None:
        self._check(state)
        if state.table.ended:
            return None
        actor = state.table.hand.game.actor(state.table.hand.state)
        return actor if actor == -1 else state.table.participants[actor]

    def betting_round(self, state: MushroomGameState) -> int:
        self._check(state)
        hand = state.table.hand
        return (state.table.hand_number - 1) * 4 + hand.state.street if hand is not None else 0

    def active_players(self, state: MushroomGameState) -> tuple[int, ...]:
        self._check(state)
        if state.table.ended:
            return ()
        hand = state.table.hand
        return tuple(state.table.participants[p] for p in hand.game.active_players(hand.state))

    def legal_actions(self, state: MushroomGameState) -> tuple[str, ...]:
        return (
            ()
            if self.actor(state) is None
            else state.table.hand.game.legal_actions(state.table.hand.state)
        )

    def chance_outcomes(self, state: MushroomGameState) -> tuple[tuple[str, float], ...]:
        if self.actor(state) != -1:
            return ()
        return state.table.hand.game.chance_outcomes(state.table.hand.state)

    def sample_chance_outcome(self, state: MushroomGameState, rng: Random) -> tuple[str, float]:
        if self.actor(state) != -1:
            raise ValueError("State is not a chance node")
        return state.table.hand.game.sample_chance_outcome(state.table.hand.state, rng)

    def transition(self, state: MushroomGameState, action: str) -> MushroomGameState:
        if self.actor(state) is None:
            raise ValueError("Cannot act on an ended session")
        table = state.table.fork()
        table.transition(action)
        public, private = state.public_history, state.private_history
        if table.hand.state.terminal:
            hand, participants = table.hand, table.participants
            award = hand.award_player
            public += (
                json.dumps(
                    {
                        "hand": table.hand_number,
                        "participants": participants,
                        "public": hand.game.public_key(hand.state),
                        "mushroomAward": hand.award,
                        "revealed": (participants[award], hand.state.hole_cards[award])
                        if award is not None
                        else None,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            hands = dict(zip(participants, hand.state.hole_cards, strict=True))
            private = tuple(
                history + (tuple(sorted(hands.get(player, ()))),)
                for player, history in enumerate(private)
            )
            if table.hand_number >= self.horizon:
                table.end_session()
            else:
                table.begin_hand()
        return MushroomGameState(self.rules_version, table, public, private)

    def public_key(self, state: MushroomGameState) -> str:
        self._check(state)
        table, hand = state.table, state.table.hand
        return mushroom_public_key(
            rules_version=self.rules_version,
            stacks=table.stacks,
            pool=table.pool,
            outstanding=table.ledger.snapshot().outstanding_contributions,
            button=table.button,
            participants=table.participants,
            waiting_for_top_up=table.waiting_for_top_up,
            hand_number=table.hand_number,
            ended=table.ended,
            history=state.public_history,
            hand=hand.game.public_key(hand.state) if hand is not None else None,
        )

    def information_key(self, state: MushroomGameState, player: int) -> str:
        self._check(state)
        if type(player) is not int or not 0 <= player < self.num_players:
            raise ValueError("Player is outside the table")
        cards = ()
        if player in state.table.participants:
            local = state.table.participants.index(player)
            cards = state.table.hand.state.hole_cards[local]
        return mushroom_information_key(
            player, state.private_history[player], cards, self.public_key(state)
        )

    def terminal_payoffs(self, state: MushroomGameState) -> tuple[float, ...]:
        if self.actor(state) is not None:
            raise ValueError("Payoffs require an ended session")
        return tuple(
            float(end - start)
            for end, start in zip(state.table.stacks, self.initial_wealth, strict=True)
        )

    def observation(self, state: MushroomGameState, player: int) -> dict:
        self.information_key(state, player)
        actor, table = self.actor(state), state.table
        hand = table.hand
        cards = []
        if player in table.participants:
            cards = list(hand.state.hole_cards[table.participants.index(player)])
        return {
            "source": "simulation",
            "gameId": self.game_id,
            "rulesVersion": self.rules_version,
            "hero": player,
            "heroCards": cards,
            "handNumber": table.hand_number,
            "board": list(hand.state.board) if hand is not None else [],
            "street": hand.state.street_name if hand is not None else None,
            "toAct": actor,
            "terminal": table.ended,
            "mushroomPool": table.pool,
            "players": [
                {
                    "seat": seat,
                    "stack": stack,
                    "participating": seat in table.participants,
                    "waitingForTopUp": seat in table.waiting_for_top_up,
                }
                for seat, stack in enumerate(table.stacks)
            ],
            "legalActions": list(self.legal_actions(state)) if actor == player else [],
            "publicHistory": list(state.public_history),
            "trainingHorizon": self.horizon,
            "quality": "finite_horizon_research_model",
        }
