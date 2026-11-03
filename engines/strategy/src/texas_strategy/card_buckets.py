"""Deterministic learned postflop buckets using visible card features only.

Features describe made rank, kickers, draws and board texture; they are not
equity, opponent ranges or betting EV. The bundled k-means codebook is a
synthetic-data engineering choice, not the unpublished Pluribus codebook.
"""

import json
from collections import Counter
from functools import lru_cache
from hashlib import sha256
from math import isfinite
from pathlib import Path

from texas_game_core.cards import RANK_VALUE, evaluate_best, validate_cards
from texas_strategy.abstraction import canonical_card_trace, preflop_class

FEATURE_VERSION = "rank-draw-texture-v1"
FEATURE_COUNT = 27
CODEBOOK = Path(__file__).with_name("mushroom_card_clusters.json")
_WINDOWS = tuple(frozenset(range(high - 4, high + 1)) for high in range(6, 15)) + (
    frozenset((14, 2, 3, 4, 5)),
)


def card_features(hole, board):
    hole, board = validate_cards(hole), validate_cards(board)
    validate_cards(hole + board)
    if len(hole) != 2 or len(board) not in (3, 4, 5):
        raise ValueError("Postflop features require two hole cards and a complete street board")
    rank = evaluate_best(hole + board)
    ranks = {RANK_VALUE[c[0]] for c in hole + board}
    private = sorted((RANK_VALUE[c[0]] for c in hole), reverse=True)
    public = [RANK_VALUE[c[0]] for c in board]
    suits = Counter(c[1] for c in hole + board)
    board_suits = Counter(c[1] for c in board)
    flush_draw = max(suits.values()) == 4
    completions = set().union(*(window - ranks for window in _WINDOWS if len(window & ranks) == 4))
    # Max over tied suits keeps the features invariant under suit relabeling.
    flush_high = max(
        (RANK_VALUE[c[0]] for c in hole if suits[c[1]] == max(suits.values())), default=0
    )
    features = (
        *(float(rank[0] == category) for category in range(9)),
        *(value / 14 for value in (*rank[1:], 0, 0, 0, 0, 0)[:5]),
        private[0] / 14,
        private[1] / 14,
        float(private[0] == private[1]),
        sum(value > max(public) for value in private) / 2,
        sum(value in public for value in private) / 2,
        max(suits.values()) / 7,
        float(flush_draw),
        flush_high / 14,
        len(completions) / 13,
        max(len(window & ranks) for window in _WINDOWS) / 5,
        max(Counter(public).values()) / 4,
        max(board_suits.values()) / 5,
        len(set(public)) / 5,
    )
    assert len(features) == FEATURE_COUNT
    return features


def squared_distance(first, second):
    return sum((a - b) ** 2 for a, b in zip(first, second, strict=True))


def nearest(features, centroids):
    return min(range(len(centroids)), key=lambda i: squared_distance(features, centroids[i]))


@lru_cache(maxsize=1)
def codebook():
    if CODEBOOK.stat().st_size > 2_000_000:
        raise ValueError("Card codebook exceeds its size limit")
    payload = json.loads(CODEBOOK.read_text())
    checksum = payload.pop("checksum")
    actual = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if checksum != actual or payload["feature_version"] != FEATURE_VERSION:
        raise ValueError("Incompatible or damaged card codebook")
    clusters = payload["centroids"]
    if set(clusters) != {"3", "4", "5"} or any(
        len(rows) != 200
        or any(
            len(row) != FEATURE_COUNT
            or any(type(v) not in (int, float) or not isfinite(v) or not 0 <= v <= 1 for v in row)
            for row in rows
        )
        for rows in clusters.values()
    ):
        raise ValueError("Invalid postflop cluster dimensions")
    return actual, clusters


@lru_cache(maxsize=8192)
def _postflop_bucket(hole, board):
    _, centroids = codebook()
    return nearest(card_features(hole, board), centroids[str(len(board))])


def card_bucket(hole, board):
    if len(hole) != 2 or len(board) not in (0, 3, 4, 5):
        raise ValueError("Card buckets require two hole cards and a complete street board")
    if not board:
        return "preflop", preflop_class(hole)
    # Canonicalization makes both the cache and the inference invariant to global suit names.
    canonical_hole, canonical_board = canonical_card_trace(hole, board)[-1]
    return len(board), _postflop_bucket(canonical_hole, canonical_board)
