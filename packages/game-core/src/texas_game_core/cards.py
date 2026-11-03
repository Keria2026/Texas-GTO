"""Standard Hold'em card ranks and bounded, reproducible pot-share equity.

Equity is an expected share at showdown, not the EV of a betting action. A
reported standard error measures sampling error under the supplied ranges; it
does not measure uncertainty about those ranges.
"""

from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from math import isfinite, sqrt
from random import Random

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = tuple(rank + suit for rank in RANKS for suit in SUITS)
RANK_VALUE = {rank: value for value, rank in enumerate(RANKS, 2)}
CATEGORY_NAMES = (
    "high_card",
    "one_pair",
    "two_pair",
    "three_of_a_kind",
    "straight",
    "flush",
    "full_house",
    "four_of_a_kind",
    "straight_flush",
)
MAX_EQUITY_SAMPLES = 100_000
Rank = tuple[int, ...]
RangeKey = str | tuple[str, str]
WeightedRange = Mapping[RangeKey, float]


def normalize_card(card: str) -> str:
    """Return a canonical two-character card, e.g. ``aS`` becomes ``As``."""
    if not isinstance(card, str) or len(card) != 2:
        raise ValueError("cards must have exactly two characters, e.g. As or Tc")
    rank, suit = card[0].upper(), card[1].lower()
    if rank not in RANKS or suit not in SUITS:
        raise ValueError(f"invalid card: {card!r}")
    return rank + suit


def validate_cards(cards: Iterable[str]) -> tuple[str, ...]:
    """Canonicalize cards and reject duplicates, including case variants."""
    result = tuple(normalize_card(card) for card in cards)
    if len(set(result)) != len(result):
        raise ValueError("duplicate cards")
    return result


def _straight_high(ranks: Iterable[int]) -> int | None:
    values = set(ranks)
    if 14 in values:
        values.add(1)
    for high in range(14, 4, -1):
        if all(value in values for value in range(high - 4, high + 1)):
            return high
    return None


def _evaluate(cards: tuple[str, ...]) -> Rank:
    """Rank validated five to seven cards directly, without subset enumeration."""
    ranks = [RANK_VALUE[card[0]] for card in cards]
    counts = Counter(ranks)
    descending = sorted(counts, reverse=True)
    flush_ranks = None
    for suit, count in Counter(card[1] for card in cards).items():
        if count >= 5:
            flush_ranks = sorted(
                (RANK_VALUE[card[0]] for card in cards if card[1] == suit), reverse=True
            )
            straight_flush = _straight_high(flush_ranks)
            if straight_flush is not None:
                return (8, straight_flush)
            break
    quads = [rank for rank in descending if counts[rank] == 4]
    if quads:
        return (7, quads[0], next(rank for rank in descending if rank != quads[0]))
    trips = [rank for rank in descending if counts[rank] >= 3]
    if trips:
        pairs = [rank for rank in descending if counts[rank] >= 2 and rank != trips[0]]
        if pairs:
            return (6, trips[0], pairs[0])
    if flush_ranks is not None:
        return (5, *flush_ranks[:5])
    straight = _straight_high(descending)
    if straight is not None:
        return (4, straight)
    if trips:
        return (3, trips[0], *(rank for rank in descending if rank != trips[0]))[:4]
    pairs = [rank for rank in descending if counts[rank] == 2]
    if len(pairs) >= 2:
        kicker = next(rank for rank in descending if rank not in pairs[:2])
        return (2, pairs[0], pairs[1], kicker)
    if pairs:
        return (1, pairs[0], *(rank for rank in descending if rank != pairs[0]))[:5]
    return (0, *descending[:5])


def evaluate_five(cards: Iterable[str]) -> Rank:
    """Comparable rank: category 0..8 followed by relevant descending kickers."""
    hand = validate_cards(cards)
    if len(hand) != 5:
        raise ValueError("evaluate_five requires exactly five cards")
    return _evaluate(hand)


def evaluate_best(cards: Iterable[str]) -> Rank:
    """Best five-card rank from five, six or seven distinct cards."""
    hand = validate_cards(cards)
    if not 5 <= len(hand) <= 7:
        raise ValueError("evaluate_best requires five to seven cards")
    return _evaluate(hand)


def category_name(rank: Rank) -> str:
    if not rank or not 0 <= rank[0] < len(CATEGORY_NAMES):
        raise ValueError("invalid hand category")
    return CATEGORY_NAMES[rank[0]]


@dataclass(frozen=True)
class EquityResult:
    equity: float
    stderr: float
    samples: int
    method: str
    seed: int
    opponents: int
    rejected_draws: int = 0
    algorithm_version: str = "card-equity-v1"


@dataclass(frozen=True)
class _RangeDistribution:
    hands: tuple[tuple[str, str], ...]
    weights: tuple[float, ...]
    cumulative: tuple[float, ...]

    def draw(self, rng: Random) -> tuple[str, str]:
        index = bisect_right(self.cumulative, rng.random() * self.cumulative[-1])
        return self.hands[min(index, len(self.hands) - 1)]


def _range_distribution(spec: WeightedRange, known: set[str]) -> _RangeDistribution:
    if not isinstance(spec, Mapping):
        raise ValueError("each opponent range must be a mapping or None")
    entries = []
    for raw_hand, raw_weight in spec.items():
        if isinstance(raw_hand, str):
            if len(raw_hand) != 4:
                raise ValueError("range strings must be exact combos, e.g. AsKh")
            cards = (raw_hand[:2], raw_hand[2:])
        elif isinstance(raw_hand, tuple) and len(raw_hand) == 2:
            cards = raw_hand
        else:
            raise ValueError("range keys must be two-card tuples or four-character combos")
        hand = tuple(sorted(validate_cards(cards)))
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (float, int)):
            raise ValueError("range weights must be finite nonnegative numbers")
        try:
            weight = float(raw_weight)
        except OverflowError as error:
            raise ValueError("range weights must be finite nonnegative numbers") from error
        if not isfinite(weight) or weight < 0:
            raise ValueError("range weights must be finite nonnegative numbers")
        if weight > 0 and not known.intersection(hand):
            entries.append((hand, weight))
    if not entries:
        raise ValueError("opponent range has no positive legal mass after known-card blockers")
    # Scale before merging reversed duplicate combos to avoid overflow.
    scale = max(weight for _, weight in entries)
    merged = {}
    for hand, weight in entries:
        merged[hand] = merged.get(hand, 0.0) + weight / scale
    hands = tuple(sorted(merged))
    weights = tuple(merged[hand] for hand in hands)
    cumulative = []
    total = 0.0
    for weight in weights:
        total += weight
        cumulative.append(total)
    return _RangeDistribution(hands, weights, tuple(cumulative))


def _pot_share(hero: tuple[str, ...], board: tuple[str, ...], holes: Sequence[tuple]) -> float:
    hero_rank = _evaluate(hero + board)
    opponent_ranks = [_evaluate(hand + board) for hand in holes]
    if any(rank > hero_rank for rank in opponent_ranks):
        return 0.0
    return 1.0 / (1 + sum(rank == hero_rank for rank in opponent_ranks))


def estimate_equity(
    hero: Iterable[str],
    board: Iterable[str] = (),
    *,
    opponents: int = 1,
    ranges: Sequence[WeightedRange | None] | None = None,
    samples: int = 2000,
    seed: int = 0,
    exact_river: bool = True,
) -> EquityResult:
    """Expected showdown pot share against independent weighted range priors.

    Joint ranges are conditioned on mutual card compatibility. Explicit ranges
    are drawn independently and incompatible joint draws rejected; sequentially
    renormalizing each weighted range would bias this distribution. ``None``
    selects the uniform legal range. Unknown board cards are sampled uniformly.
    Heads-up river is enumerated exactly by default. All other cases are bounded
    Monte Carlo, with at most ``max(10000, samples * 100)`` joint draw attempts.
    All participants are eligible for the same pot; side pots require separate
    calls with their own eligible participants.
    """
    hero_cards = validate_cards(hero)
    board_cards = validate_cards(board)
    if len(hero_cards) != 2 or len(board_cards) not in (0, 3, 4, 5):
        raise ValueError("hero requires two cards; board requires zero, three, four or five")
    known = set(validate_cards(hero_cards + board_cards))
    if type(opponents) is not int or not 1 <= opponents <= 7:
        raise ValueError("opponents must be an integer from one to seven")
    if type(samples) is not int or not 1 <= samples <= MAX_EQUITY_SAMPLES:
        raise ValueError(f"samples must be an integer from one to {MAX_EQUITY_SAMPLES}")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if type(exact_river) is not bool:
        raise ValueError("exact_river must be boolean")
    if ranges is None:
        ranges = (None,) * opponents
    if len(ranges) != opponents:
        raise ValueError("ranges must contain one entry per opponent")
    distributions = tuple(
        None if spec is None else _range_distribution(spec, known) for spec in ranges
    )
    available = tuple(card for card in DECK if card not in known)
    if len(board_cards) == 5 and opponents == 1 and exact_river:
        distribution = distributions[0]
        hands = tuple(combinations(available, 2)) if distribution is None else distribution.hands
        weights = (1.0,) * len(hands) if distribution is None else distribution.weights
        share = sum(
            weight * _pot_share(hero_cards, board_cards, (hand,))
            for hand, weight in zip(hands, weights, strict=True)
        ) / sum(weights)
        return EquityResult(share, 0.0, len(hands), "exact_river", seed, opponents)
    if samples < 2:
        raise ValueError("Monte Carlo equity requires at least two samples for standard error")
    rng = Random(seed)
    mean = squared_deviations = 0.0
    completed = rejected = 0
    uniform_count = sum(distribution is None for distribution in distributions)
    draw_count = uniform_count * 2 + 5 - len(board_cards)
    for _ in range(max(10_000, samples * 100)):
        occupied = set(known)
        holes = []
        for distribution in distributions:
            if distribution is None:
                continue
            hand = distribution.draw(rng)
            if occupied.intersection(hand):
                rejected += 1
                break
            occupied.update(hand)
            holes.append(hand)
        else:
            remainder = [card for card in available if card not in occupied]
            draw = rng.sample(remainder, draw_count)
            for index in range(uniform_count):
                holes.append(tuple(draw[index * 2 : index * 2 + 2]))
            final_board = board_cards + tuple(draw[uniform_count * 2 :])
            share = _pot_share(hero_cards, final_board, holes)
            completed += 1
            delta = share - mean
            mean += delta / completed
            squared_deviations += delta * (share - mean)
            if completed == samples:
                stderr = sqrt(max(0.0, squared_deviations) / (samples - 1) / samples)
                return EquityResult(mean, stderr, samples, "monte_carlo", seed, opponents, rejected)
    raise ValueError("joint opponent ranges exceeded compatibility rejection budget")
