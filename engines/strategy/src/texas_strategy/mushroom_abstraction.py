"""Reusable, versioned mushroom contexts over the unchanged full rules engine.

Preflop cards use 169 classes; each postflop street has 200 learned buckets.
Chips are bucketed, and completed-hand observations are forgotten after their monetary
effects enter the table/escrow state. This is an imperfect-recall, finite-horizon
blueprint approximation, not a convergence or live-strategy quality guarantee.
Actual stacks, payments, legal wagers and terminal net points remain exact.
"""

import json
from bisect import bisect_right
from dataclasses import dataclass
from hashlib import sha256

from texas_game_core.mushroom_game import MushroomGameState, MushroomSessionGame
from texas_strategy.card_buckets import card_bucket, codebook

VERSION = "mushroom-context-v1"
# Keep fee/straddle boundaries; larger depth bands are an explicit blueprint approximation.
CHIP_EDGES = tuple(range(9)) + (
    20,
    21,
    28,
    40,
    41,
    80,
    160,
    320,
    400,
    800,
    1600,
    3200,
    6400,
    12800,
    25600,
    51200,
)


def chip_bucket(amount: int) -> int:
    if type(amount) is not int or amount < 0:
        raise ValueError("Chip buckets require nonnegative integers")
    return bisect_right(CHIP_EDGES, amount) - 1


def context_action_map(game, hand) -> dict[str, str]:
    """Label concrete legal wagers using public amounts only, preserving their order."""
    if game.actor(hand) in (None, -1):
        return {}
    actual = game.legal_actions(hand)
    result = {action: action for action in actual if not action.startswith("raise_to:")}
    maximum = hand.street_bets[hand.to_act] + hand.stacks[hand.to_act]
    minimum = game.minimum_raise_to(hand)
    pot = hand.pot + game.amount_to_call(hand)
    candidates = (
        ("raise_min", minimum),
        ("raise_half_pot", min(maximum, max(minimum, hand.current_bet + pot // 2))),
        ("raise_pot", min(maximum, max(minimum, hand.current_bet + pot))),
        ("all_in", maximum),
    )
    for action in actual:
        if action.startswith("raise_to:"):
            target = int(action.split(":", 1)[1])
            label = (
                "all_in"
                if target == maximum
                else next(name for name, amount in candidates if amount == target)
            )
            result[label] = action
    return result


def encode_context(
    *,
    num_players,
    physical,
    button,
    betting_round,
    cards,
    hand,
    stacks,
    outstanding,
    participants,
    waiting_for_top_up,
    pool,
    actions,
    legal_actions,
    ante=0,
):
    """Pure legacy encoding; callers validate facts before crossing an external boundary.

    ``hand`` supplies public betting fields only. The observing player's cards
    are passed separately, so this function never reads other players' cards.
    """
    trace = (
        card_bucket(cards, hand.board)
        if len(cards) == 2
        else ("private_deal", tuple(card[0] for card in cards))
    )
    order = tuple((button + i) % num_players for i in range(num_players))
    local_seats = {p: i for i, p in enumerate(participants)}
    players = []
    for seat in order:
        index = local_seats.get(seat)
        players.append(
            (
                chip_bucket(stacks[seat]),
                chip_bucket(outstanding[seat]),
                seat in waiting_for_top_up,
                None
                if index is None
                else (
                    hand.folded[index],
                    chip_bucket(hand.contributions[index]),
                    chip_bucket(hand.street_bets[index]),
                    None if hand.acted_to[index] is None else chip_bucket(hand.acted_to[index]),
                ),
            )
        )
    context = (
        VERSION,
        betting_round,
        (physical - button) % num_players,
        trace,
        players,
        chip_bucket(pool),
        chip_bucket(hand.pot),
        chip_bucket(hand.current_bet),
        chip_bucket(hand.last_full_raise),
        tuple((participants[p] - button) % num_players for p in hand.pending),
        actions,
        tuple(legal_actions),
    )
    key = sha256(json.dumps(context, separators=(",", ":")).encode()).hexdigest()
    # Retained no-ante rows must never supply coverage for the new rules.
    return f"ante{ante}:{key}" if ante else key


@dataclass(frozen=True)
class MushroomContextState:
    underlying: MushroomGameState
    actions: tuple[tuple[int, int, str], ...] = ()


class MushroomContextGame:
    game_id = "mushroom"
    supports_exact_enumeration = False
    abstraction_version = VERSION

    def __init__(self, base_game: MushroomSessionGame):
        if not isinstance(base_game, MushroomSessionGame):
            raise ValueError("Mushroom context requires a mushroom session game")
        self.base_game = base_game
        self.training_key_prefix = f"ante{base_game.ante}:" if base_game.ante else ""
        self.num_players = base_game.num_players
        self.final_betting_round = base_game.final_betting_round
        # Reusing a policy across roots must not silently change deterministic continuation.
        context = {"stacks": base_game.initial_stacks, "button": base_game.button}
        if base_game.ante:
            context["ante"] = base_game.ante
        if any(base_game.initial_outstanding):
            context["openingOutstanding"] = base_game.initial_outstanding
        if base_game.initial_waiting_for_top_up:
            context["openingWaiting"] = base_game.initial_waiting_for_top_up
        self.checkpoint_context = json.dumps(
            context,
            sort_keys=True,
            separators=(",", ":"),
        )
        # Opening finances and eligibility bind continuation, not a separate policy family.
        rules = json.loads(base_game.rules_version.split(":", 1)[1])
        for field in (
            "stacks",
            "button",
            "informationEncoding",
            "openingOutstanding",
            "openingWaiting",
        ):
            rules.pop(field, None)
        self.rules_version = (
            VERSION
            + ":"
            + json.dumps(
                {
                    "rules": rules,
                    "chipEdges": CHIP_EDGES,
                    "cards": "postflop-200:" + codebook()[0],
                    "recall": "current_card_bucket_hand_actions_bucketed_finances",
                    "completedHands": "monetary_state_only",
                    "actions": "fold_check_call_min_half_pot_pot_all_in_v1",
                    "seats": "relative_to_initial_button",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    def _seat(self, physical: int) -> int:
        return (physical - self.base_game.button) % self.num_players

    def _physical(self, player: int) -> int:
        if type(player) is not int or not 0 <= player < self.num_players:
            raise ValueError("Player is outside the table")
        return (player + self.base_game.button) % self.num_players

    def initial_state(self):
        return MushroomContextState(self.base_game.initial_state())

    def actor(self, state):
        actor = self.base_game.actor(state.underlying)
        return actor if actor in (None, -1) else self._seat(actor)

    def active_players(self, state):
        return tuple(sorted(self._seat(p) for p in self.base_game.active_players(state.underlying)))

    def betting_round(self, state):
        return self.base_game.betting_round(state.underlying)

    def chance_outcomes(self, state):
        return self.base_game.chance_outcomes(state.underlying)

    def sample_chance_outcome(self, state, rng):
        return self.base_game.sample_chance_outcome(state.underlying, rng)

    def action_map(self, state) -> dict[str, str]:
        actor = self.actor(state)
        if actor is None or actor == -1:
            return {}
        hand = state.underlying.table.hand
        return context_action_map(hand.game, hand.state)

    def legal_actions(self, state):
        if self.actor(state) == -1:
            return self.base_game.legal_actions(state.underlying)
        return tuple(self.action_map(state))

    def concrete_action(self, state, action):
        return self.action_map(state).get(action, action)

    def transition(self, state, action):
        actor, table = self.actor(state), state.underlying.table
        actions = state.actions
        if actor is not None and actor >= 0:
            mapping = self.action_map(state)
            actual = mapping.get(action, action)
            label = next((name for name, value in mapping.items() if value == actual), actual)
            relative = (self._physical(actor) - table.button) % self.num_players
            actions += ((table.hand.state.street, relative, label),)
        else:
            actual = action
        underlying = self.base_game.transition(state.underlying, actual)
        if underlying.table.hand_number != table.hand_number:
            actions = ()
        return MushroomContextState(underlying, actions)

    def information_key(self, state, player):
        physical = self._physical(player)
        self.base_game._check(state.underlying)
        table = state.underlying.table
        if physical not in table.participants:
            raise ValueError("Observer is not participating in this hand")
        hand = table.hand.state
        local = table.participants.index(physical)
        outstanding = dict(table.ledger.snapshot().outstanding_contributions)
        return encode_context(
            num_players=self.num_players,
            physical=physical,
            button=table.button,
            betting_round=self.betting_round(state),
            cards=hand.hole_cards[local],
            hand=hand,
            stacks=table.stacks,
            outstanding=tuple(outstanding.get(str(seat), 0) for seat in range(self.num_players)),
            participants=table.participants,
            waiting_for_top_up=table.waiting_for_top_up,
            pool=table.pool,
            actions=state.actions,
            legal_actions=self.action_map(state),
            ante=self.base_game.ante,
        )

    def public_key(self, state):
        return self.base_game.public_key(state.underlying)

    def search_information_key(self, state, player):
        # The online subgame must distinguish actual hands even when the blueprint merges them.
        return self.base_game.information_key(state.underlying, self._physical(player))

    def terminal_payoffs(self, state):
        values = self.base_game.terminal_payoffs(state.underlying)
        return tuple(values[self._physical(player)] for player in range(self.num_players))

    def observation(self, state, player):
        result = self.base_game.observation(state.underlying, self._physical(player))
        result.update(
            rulesVersion=self.rules_version,
            hero=player,
            toAct=self.actor(state),
            legalActions=list(self.legal_actions(state)) if self.actor(state) == player else [],
            actionAmounts=self.action_map(state) if self.actor(state) == player else {},
            quality="abstract_finite_horizon_research_model",
        )
        for item in result["players"]:
            item["physicalSeat"] = item["seat"]
            item["seat"] = self._seat(item["seat"])
        return result
