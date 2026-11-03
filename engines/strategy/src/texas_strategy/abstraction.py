"""Exact suit isomorphism for the first Hold'em blueprint abstraction.

This module merges only global suit relabelings of a player's observable cards.
Ranks, suit relationships, card blockers, action history and chip commitments
remain intact. In particular this is not effective hand strength, a learned
potential histogram, or a reproduction of Pluribus's unpublished bucket setup.

Before the flop the quotient has the familiar 169 pair/suited/offsuit classes.
After the flop it keeps the complete observable cards up to suit isomorphism,
including the flop/turn/river reveal boundaries. Earlier street observations
are retained explicitly so that an information set cannot forget its previous
card abstraction. Training still samples the full underlying legal card game.
"""

import json
from functools import lru_cache
from itertools import permutations
from typing import Any

from texas_game_core.cards import RANK_VALUE, SUITS, validate_cards

ABSTRACTION_VERSION = "suit-canonical-v1"
_SUIT_MAPS = tuple(
    dict(zip(SUITS, permutation, strict=True)) for permutation in permutations(SUITS)
)


def preflop_class(hole_cards: tuple[str, str]) -> str:
    """One of 169 rank-pair classes, with no postflop-strength assertion."""
    cards = validate_cards(hole_cards)
    if len(cards) != 2:
        raise ValueError("Preflop classification requires exactly two distinct cards")
    high, low = sorted(cards, key=lambda card: RANK_VALUE[card[0]], reverse=True)
    if high[0] == low[0]:
        return high[0] + low[0]
    return high[0] + low[0] + ("s" if high[1] == low[1] else "o")


@lru_cache(maxsize=8192)
def _canonical_visible(
    hole_cards: tuple[str, str], board: tuple[str, ...]
) -> tuple[tuple[str, str], tuple[str, ...]]:
    """Lexicographic minimum over one common suit permutation of all cards.

    Hole-card order and order *within the flop* are immaterial. Turn and river
    positions remain ordered so prior public observations are reconstructible.
    """

    def apply(mapping: dict[str, str]) -> tuple[tuple[str, str], tuple[str, ...]]:
        hole = tuple(sorted(card[0] + mapping[card[1]] for card in hole_cards))
        flop = tuple(sorted(card[0] + mapping[card[1]] for card in board[:3]))
        later = tuple(card[0] + mapping[card[1]] for card in board[3:])
        return hole, flop + later

    return min(apply(mapping) for mapping in _SUIT_MAPS)


def canonical_card_trace(
    hole_cards: tuple[str, str], board: tuple[str, ...] = ()
) -> tuple[tuple[tuple[str, str], tuple[str, ...]], ...]:
    """Canonical observable-card history, computed solely from current known cards.

    An empty board gives one preflop observation. A complete flop and each later
    card append their street observation. Partial flop states are supported for
    debugging/chance boundaries, but do not create player actions in Hold'em.
    Each earlier observation is recomputed using only its own board prefix;
    future cards cannot change an earlier bucket.
    """
    hole = validate_cards(hole_cards)
    public = validate_cards(board)
    if len(hole) != 2 or len(public) > 5:
        raise ValueError("Card abstraction requires two hole cards and at most five board cards")
    validate_cards(hole + public)
    hole = tuple(sorted(hole))
    lengths = [0]
    if public:
        lengths.append(min(3, len(public)))
    lengths.extend(range(4, len(public) + 1))
    return tuple(_canonical_visible(hole, public[:length]) for length in lengths)


class SuitCanonicalHoldem:
    """A policy-information wrapper; the underlying states and rules are unchanged.

    ``information_key`` reads only this player's hole cards and the game's
    public state. It never uses other players' hidden cards or future boards.
    Global suit relabeling is a symmetry of standard Hold'em, so this quotient
    removes redundant suit identities rather than approximating hand strength.
    Multiplayer convergence and useful full-game coverage remain unproven.

    Public roots retain the real public board via delegated ``public_key``;
    suit-dependent canonical labels are private information keys, not a new
    public observation. Blueprint/checkpoint metadata include the abstraction
    version in ``rules_version`` and cannot load into an unwrapped game.
    """

    game_id = "holdem"
    abstraction_version = ABSTRACTION_VERSION

    def __init__(self, base_game: Any) -> None:
        if getattr(base_game, "game_id", None) != "holdem" or isinstance(
            base_game, SuitCanonicalHoldem
        ):
            raise ValueError("SuitCanonicalHoldem requires an unwrapped standard Hold'em game")
        self.base_game = base_game
        self.rules_version = f"{base_game.rules_version}|{ABSTRACTION_VERSION}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base_game, name)

    def information_key(self, state: Any, player: int) -> str:
        if type(player) is not int or not 0 <= player < self.num_players:
            raise ValueError("Invalid observing player")
        # public_key performs the underlying rule/state compatibility checks.
        public = json.loads(self.base_game.public_key(state))
        board = tuple(public.pop("board"))
        trace = canonical_card_trace(tuple(state.hole_cards[player]), board)
        return json.dumps(
            [ABSTRACTION_VERSION, player, trace, public], sort_keys=True, separators=(",", ":")
        )
