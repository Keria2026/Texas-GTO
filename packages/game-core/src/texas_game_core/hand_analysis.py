"""Current-street hand order, nuts bounds and explicit beating combinations.

Theoretical nuts exclude only board cards. Blocker-aware analysis also removes
hero's cards. A supplied opponent range is a separate, conditional hypothesis;
being unbeaten inside that range is not the same as holding theoretical nuts.
This module does not estimate future equity, outs or betting-action EV.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from itertools import combinations
from math import fsum, isfinite

from texas_game_core.cards import (
    DECK,
    Rank,
    WeightedRange,
    category_name,
    evaluate_best,
    validate_cards,
)

MODEL_VERSION = "current-hand-strength-v1"
MAX_BEATING_COMBOS = 1326


@dataclass(frozen=True)
class NutsBound:
    best_rank: Rank
    best_category: str
    hero_is_nuts: bool
    combinations_evaluated: int


@dataclass(frozen=True)
class NutsAnalysis:
    theoretical: NutsBound
    blocker_aware: NutsBound
    range: NutsBound | None


@dataclass(frozen=True)
class BeatingCombination:
    cards: tuple[str, str]
    rank: Rank
    category: str
    normalized_weight: float


@dataclass(frozen=True)
class CurrentComparison:
    range_source: str
    combinations_evaluated: int
    best_rank: Rank
    best_category: str
    current_ahead_weight: float
    current_tied_weight: float
    current_behind_weight: float
    ahead_combinations: int
    tied_combinations: int
    beating_combinations_count: int
    beating_combinations: tuple[BeatingCombination, ...]
    beating_combinations_truncated: bool
    normalization_underflow_combinations: int = 0
    weight_normalization: str = "sum_to_one_after_known_card_blockers"


@dataclass(frozen=True)
class HandStrengthResult:
    hero: tuple[str, ...]
    board: tuple[str, ...]
    street: str
    current_rank: Rank | None
    current_category: str | None
    nuts: NutsAnalysis | None
    opponents: CurrentComparison | None
    range_comparison: CurrentComparison | None
    warnings: tuple[str, ...]
    model_version: str = MODEL_VERSION
    rules_version: str = "standard-high-holdem-v1"
    evaluation_scope: str = "current_street_only"
    equity_computed: bool = False
    action_ev_computed: bool = False

    def to_dict(self) -> dict:
        """Return a detached JSON object, with ranks and hands encoded as lists."""
        return json.loads(json.dumps(asdict(self), allow_nan=False))


def _normalize_range(spec: WeightedRange, known: set[str]) -> dict[tuple[str, str], float]:
    if not isinstance(spec, Mapping):
        raise ValueError("opponent_range must map exact two-card combinations to weights")
    entries = []
    for key, raw_weight in spec.items():
        if isinstance(key, str) and len(key) == 4:
            cards = (key[:2], key[2:])
        elif isinstance(key, tuple) and len(key) == 2:
            cards = key
        else:
            raise ValueError("range keys must be exact combos, e.g. AsKh or ('As', 'Kh')")
        hand = tuple(sorted(validate_cards(cards)))
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (float, int)):
            raise ValueError("range weights must be finite nonnegative numbers")
        try:
            weight = float(raw_weight)
        except OverflowError as error:
            raise ValueError("range weights must be finite nonnegative numbers") from error
        if not isfinite(weight) or weight < 0:
            raise ValueError("range weights must be finite nonnegative numbers")
        if weight and not known.intersection(hand):
            entries.append((hand, weight))
    if not entries:
        raise ValueError("opponent range has no positive legal mass after known-card blockers")
    # Rescale before merging reversed duplicate combo keys to avoid overflow.
    scale = max(weight for _, weight in entries)
    merged = {}
    for hand, weight in entries:
        merged[hand] = merged.get(hand, 0.0) + weight / scale
    total = fsum(merged.values())
    # Preserve originally positive legal combos even when tiny normalized
    # probabilities underflow. Dropping one could falsely declare range nuts.
    return {hand: value / total for hand, value in merged.items()}


def _comparison(
    hero_rank: Rank,
    ranks: Mapping[tuple[str, str], Rank],
    weights: Mapping[tuple[str, str], float],
    *,
    source: str,
    limit: int,
) -> CurrentComparison:
    ahead = [hand for hand in weights if ranks[hand] < hero_rank]
    tied = [hand for hand in weights if ranks[hand] == hero_rank]
    beating = [hand for hand in weights if ranks[hand] > hero_rank]
    beating.sort(key=lambda hand: (weights[hand], ranks[hand], hand), reverse=True)
    best_rank = max(ranks[hand] for hand in weights)
    return CurrentComparison(
        range_source=source,
        combinations_evaluated=len(weights),
        best_rank=best_rank,
        best_category=category_name(best_rank),
        current_ahead_weight=fsum(weights[hand] for hand in ahead),
        current_tied_weight=fsum(weights[hand] for hand in tied),
        current_behind_weight=fsum(weights[hand] for hand in beating),
        ahead_combinations=len(ahead),
        tied_combinations=len(tied),
        beating_combinations_count=len(beating),
        beating_combinations=tuple(
            BeatingCombination(hand, ranks[hand], category_name(ranks[hand]), weights[hand])
            for hand in beating[:limit]
        ),
        beating_combinations_truncated=len(beating) > limit,
        normalization_underflow_combinations=sum(weight == 0 for weight in weights.values()),
    )


def _nuts_bound(hero_rank: Rank, best_rank: Rank, count: int) -> NutsBound:
    return NutsBound(best_rank, category_name(best_rank), hero_rank >= best_rank, count)


def analyze_hand_strength(
    hero: Iterable[str],
    board: Iterable[str] = (),
    *,
    opponent_range: WeightedRange | None = None,
    max_beating_combos: int = 50,
) -> HandStrengthResult:
    """Exhaustively compare current hands, with bounded counterexample output.

    The largest domain is 1,176 two-card combinations on the flop. The complete
    domain is always evaluated, including when counterexamples are truncated.
    ``opponents`` is uniform over legal combinations after hero/board blockers.
    ``range_comparison`` separately normalizes a supplied range after blockers.
    Its weights are current ahead/tied/behind mass, not final showdown equity.
    ``max_beating_combos`` controls each comparison's example-list length only.
    No private opponent cards or future board cards are accepted as input.
    """
    hero_cards = validate_cards(hero)
    board_cards = validate_cards(board)
    if len(hero_cards) != 2 or len(board_cards) not in (0, 3, 4, 5):
        raise ValueError("hero requires two cards; board requires zero, three, four or five")
    known = set(validate_cards(hero_cards + board_cards))
    if type(max_beating_combos) is not int or not 0 <= max_beating_combos <= MAX_BEATING_COMBOS:
        raise ValueError(f"max_beating_combos must be an integer from zero to {MAX_BEATING_COMBOS}")
    supplied_weights = None if opponent_range is None else _normalize_range(opponent_range, known)
    street = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}[len(board_cards)]
    if not board_cards:
        return HandStrengthResult(
            hero_cards,
            board_cards,
            street,
            None,
            None,
            None,
            None,
            None,
            ("翻前不足五张牌，当前五张牌型与 nuts 未定义；未计算 equity 或行动 EV。",),
        )
    current_rank = evaluate_best(hero_cards + board_cards)
    # Theoretical nuts intentionally allow hero's cards in the hypothetical
    # opponent pair. This is the board-only counterfactual, not a legal opponent
    # range in the actual hand.
    board_set = set(board_cards)
    board_legal = tuple(card for card in DECK if card not in board_set)
    all_ranks = {
        tuple(sorted(hand)): evaluate_best(hand + board_cards)
        for hand in combinations(board_legal, 2)
    }
    theoretical_best = max(all_ranks.values())
    legal_ranks = {hand: rank for hand, rank in all_ranks.items() if not known.intersection(hand)}
    uniform_weights = dict.fromkeys(legal_ranks, 1.0 / len(legal_ranks))
    opponents = _comparison(
        current_rank,
        legal_ranks,
        uniform_weights,
        source="uniform_legal_combinations",
        limit=max_beating_combos,
    )
    range_comparison = (
        None
        if supplied_weights is None
        else _comparison(
            current_rank,
            legal_ranks,
            supplied_weights,
            source="provided_weighted_combinations",
            limit=max_beating_combos,
        )
    )
    nuts = NutsAnalysis(
        _nuts_bound(current_rank, theoretical_best, len(all_ranks)),
        _nuts_bound(current_rank, opponents.best_rank, len(legal_ranks)),
        None
        if range_comparison is None
        else _nuts_bound(
            current_rank, range_comparison.best_rank, range_comparison.combinations_evaluated
        ),
    )
    warnings = ["比较权重描述当前领先、平局或落后，不是下注行动 EV；未计算未来 outs。"]
    if len(board_cards) < 5:
        warnings.append("当前 nuts 或当前领先不保证后续公共牌发完仍然领先；本结果不是最终 equity。")
    if opponent_range is not None:
        warnings.append(
            "指定范围仅是输入假设；范围内 nuts 不代表理论 nuts，未估计范围模型不确定性。"
        )
    if range_comparison is not None and range_comparison.normalization_underflow_combinations:
        warnings.append("部分正权重归一化后低于浮点精度；这些组合仍计入可行范围、nuts 与组合计数。")
    return HandStrengthResult(
        hero_cards,
        board_cards,
        street,
        current_rank,
        category_name(current_rank),
        nuts,
        opponents,
        range_comparison,
        tuple(warnings),
    )
