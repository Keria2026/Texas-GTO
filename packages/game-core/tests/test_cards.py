from collections import Counter
from itertools import combinations
from random import Random

import pytest

from texas_game_core.cards import (
    DECK,
    category_name,
    estimate_equity,
    evaluate_best,
    evaluate_five,
    normalize_card,
    validate_cards,
)


@pytest.mark.parametrize(
    ("cards", "rank"),
    [
        ("As Kd Qh Jc 9s", (0, 14, 13, 12, 11, 9)),
        ("As Ad Qh Jc 9s", (1, 14, 12, 11, 9)),
        ("As Ad Qh Qc 9s", (2, 14, 12, 9)),
        ("As Ad Ah Jc 9s", (3, 14, 11, 9)),
        ("As 2d 3h 4c 5s", (4, 5)),
        ("As Qs 9s 5s 2s", (5, 14, 12, 9, 5, 2)),
        ("As Ad Ah Jc Js", (6, 14, 11)),
        ("As Ad Ah Ac Js", (7, 14, 11)),
        ("As Ks Qs Js Ts", (8, 14)),
    ],
)
def test_all_categories_and_kickers(cards, rank):
    assert evaluate_five(cards.split()) == rank
    assert category_name(rank)


def test_best_of_seven_handles_competing_full_houses_pairs_and_wheel():
    assert evaluate_best("As Ad Ah Ks Kd Kh 2c".split()) == (6, 14, 13)
    assert evaluate_best("As Ad Ks Kd Qs Qd Jh".split()) == (2, 14, 13, 12)
    assert evaluate_best("As 2s 3s 4s 5s 6s Kd".split()) == (8, 6)
    assert evaluate_best("As Ad Ah Ac Kd Qc Js".split()) == (7, 14, 13)


def test_board_plays_and_kicker_comparison():
    board = "As Ks Qs Js Ts".split()
    assert evaluate_best(board + ["2d", "3d"]) == evaluate_best(board + ["Ac", "Ad"])
    assert evaluate_best("As Ad Kh Qs Jd 3c 2c".split()) > evaluate_best(
        "Ac Ah Ks Qh Td 4d 3h".split()
    )


@pytest.mark.parametrize("card", ["10s", "1s", "Az", " As", "", None, 42])
def test_invalid_card_rejected(card):
    with pytest.raises(ValueError):
        normalize_card(card)


def test_normalization_duplicates_and_required_card_count():
    assert normalize_card("aS") == "As"
    assert len(DECK) == len(set(DECK)) == 52
    with pytest.raises(ValueError, match="duplicate"):
        validate_cards(["As", "aS"])
    with pytest.raises(ValueError):
        evaluate_best(["As", "Kd"])
    with pytest.raises(ValueError):
        evaluate_five("As Ks Qs Js Ts 9s".split())


def _independent_five(cards):
    # Slow subset oracle uses sorted multiplicity groups and explicit straight
    # sequences, independently of the production direct seven-card evaluator.
    values = sorted("23456789TJQKA".index(card[0]) + 2 for card in cards)
    groups = sorted(((values.count(rank), rank) for rank in set(values)), reverse=True)
    multiplicities = tuple(count for count, _ in groups)
    kickers = tuple(rank for _, rank in groups)
    flush = len({card[1] for card in cards}) == 1
    straight = values == list(range(values[0], values[0] + 5))
    wheel = values == [2, 3, 4, 5, 14]
    high = 5 if wheel else values[-1]
    if flush and (straight or wheel):
        return (8, high)
    if multiplicities == (4, 1):
        return (7, *kickers)
    if multiplicities == (3, 2):
        return (6, *kickers)
    if flush:
        return (5, *reversed(values))
    if straight or wheel:
        return (4, high)
    category = {(3, 1, 1): 3, (2, 2, 1): 2, (2, 1, 1, 1): 1}.get(multiplicities, 0)
    return (category, *kickers)


def test_seven_card_evaluator_against_independent_subset_oracle():
    rng = Random(98234)
    categories = Counter()
    for _ in range(1000):
        cards = rng.sample(DECK, 7)
        expected = max(_independent_five(hand) for hand in combinations(cards, 5))
        actual = evaluate_best(cards)
        assert actual == expected
        categories[actual[0]] += 1
    assert len(categories) >= 7


def test_exact_river_uniform_board_tie():
    result = estimate_equity(["2c", "3c"], "As Ks Qs Js Ts".split())
    assert result.equity == 0.5
    assert result.stderr == 0.0
    assert result.samples == 990
    assert result.method == "exact_river"


def test_exact_weighted_river_filters_blockers_and_applies_tie_share():
    # Hero has queens full. Opponent 22 loses; KK wins; remaining queens tie.
    result = estimate_equity(
        ["Qh", "2h"],
        "Qs Qd Kc Ks 3c".split(),
        ranges=[{"2c2d": 2, "KhKd": 1, "Qc4c": 1, "Qh5c": 10000}],
    )
    assert result.equity == 0.625
    assert result.samples == 3
    assert result.stderr == 0


def test_multiplayer_board_tie_shares_and_seeded_sampling():
    result = estimate_equity(["2c", "3c"], "As Ks Qs Js Ts".split(), opponents=5, samples=120)
    assert result.equity == pytest.approx(1 / 6)
    assert result.stderr == pytest.approx(0, abs=1e-9)
    first = estimate_equity(["As", "Ah"], samples=400, seed=71)
    assert first == estimate_equity(["As", "Ah"], samples=400, seed=71)
    assert 0.77 < first.equity < 0.92
    assert 0 < first.stderr < 0.03


def test_explicit_joint_ranges_condition_on_compatibility_without_sequential_bias():
    # Three legal equally weighted assignments: (A,C), (A,D), (B,D).
    # (B,C) conflicts on Ac. Hero beats (A,C), loses the other two. A sequential
    # range-renormalization sampler would incorrectly return 1/4 instead of 1/3.
    result = estimate_equity(
        ["Kh", "Kd"],
        "2h 4s 7d 9c Js".split(),
        opponents=2,
        ranges=[{"3c5c": 1, "Ac8c": 1}, {"AcQc": 1, "JhJd": 1}],
        samples=6000,
        seed=555,
    )
    assert result.equity == pytest.approx(1 / 3, abs=0.025)
    assert result.rejected_draws > 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hero": ["As", "As"]},
        {"hero": ["As", "Kd"], "board": ["As", "2c", "3d"]},
        {"hero": ["As", "Kd"], "board": ["2c"]},
        {"hero": ["As", "Kd"], "ranges": [{"AsKh": 1}]},
        {"hero": ["As", "Kd"], "ranges": [{"AcKh": -1}]},
        {"hero": ["As", "Kd"], "ranges": [{"AcKh": float("nan")}]},
        {"hero": ["As", "Kd"], "ranges": [{"AcKh": float("inf")}]},
        {"hero": ["As", "Kd"], "ranges": [{"AcAc": 1}]},
        {"hero": ["As", "Kd"], "ranges": []},
        {"hero": ["As", "Kd"], "samples": 0},
        {"hero": ["As", "Kd"], "samples": 1},
        {"hero": ["As", "Kd"], "samples": True},
        {"hero": ["As", "Kd"], "opponents": 6},
    ],
)
def test_invalid_equity_inputs_rejected(kwargs):
    with pytest.raises(ValueError):
        estimate_equity(**kwargs)


def test_impossible_joint_ranges_fail_with_bounded_rejection():
    with pytest.raises(ValueError, match="compatibility"):
        estimate_equity(["As", "Kd"], opponents=2, ranges=[{"AcKh": 1}, {"AcQh": 1}], samples=2)


def test_reversed_combo_weights_merge_without_overflow():
    result = estimate_equity(
        ["Qh", "2h"],
        "Qs Qd Kc Ks 3c".split(),
        ranges=[{("2c", "2d"): 1e308, ("2d", "2c"): 1e308, "KhKd": 1e308}],
    )
    assert result.equity == pytest.approx(2 / 3)
    assert result.samples == 2


def test_turn_sampling_matches_exact_river_combinatorics():
    # AA versus KK on 2345: six of 44 remaining river cards create a board
    # straight (the two remaining aces or four sixes). The other 38 win outright.
    result = estimate_equity(
        ["As", "Ah"],
        "2c 3d 4h 5s".split(),
        ranges=[{"KsKh": 1}],
        samples=5000,
        seed=37,
    )
    assert result.equity == pytest.approx(41 / 44, abs=0.012)
    assert result.stderr > 0
