import json
from dataclasses import FrozenInstanceError

import pytest

from texas_game_core.hand_analysis import analyze_hand_strength


def test_preflop_does_not_invent_a_five_card_rank_nuts_or_equity():
    result = analyze_hand_strength(["As", "Ah"])
    assert result.street == "preflop"
    assert result.current_rank is result.current_category is result.nuts is None
    assert result.opponents is result.range_comparison is None
    assert not result.equity_computed
    assert not result.action_ev_computed
    assert result.warnings
    assert result.model_version


def test_board_royal_flush_ties_every_legal_hand():
    result = analyze_hand_strength(["2c", "3d"], "As Ks Qs Js Ts".split())
    assert result.current_rank == (8, 14)
    assert result.current_category == "straight_flush"
    assert result.nuts.theoretical.hero_is_nuts
    assert result.nuts.blocker_aware.hero_is_nuts
    assert result.nuts.range is None
    assert result.opponents.combinations_evaluated == 990
    assert result.opponents.current_tied_weight == pytest.approx(1)
    assert result.opponents.current_ahead_weight == result.opponents.current_behind_weight == 0
    assert result.opponents.beating_combinations_count == 0
    assert result.opponents.beating_combinations == ()


def test_theoretical_nuts_and_blocker_aware_bound_are_different_questions():
    # JsTs would make a J-high straight flush, but hero owns Ts. Hero's T-high
    # straight flush is therefore currently unbeatable by any legal opponent.
    result = analyze_hand_strength(["Ts", "Ah"], "6s 7s 8s 9s".split())
    assert result.current_rank == (8, 10)
    assert result.nuts.theoretical.best_rank == (8, 11)
    assert result.nuts.theoretical.combinations_evaluated == 1128
    assert not result.nuts.theoretical.hero_is_nuts
    assert result.nuts.blocker_aware.best_rank == (8, 9)
    assert result.nuts.blocker_aware.combinations_evaluated == 1035
    assert result.nuts.blocker_aware.hero_is_nuts
    assert result.opponents.beating_combinations_count == 0
    assert result.opponents.current_ahead_weight == pytest.approx(1)


def test_explicit_range_nuts_do_not_replace_all_legal_opponent_nuts():
    result = analyze_hand_strength(
        ["Qh", "2h"],
        "Qs Qd Kc Ks 3c".split(),
        opponent_range={"2c2d": 2, "Qc4c": 1, "Qh5c": 10_000},
    )
    assert result.current_rank == (6, 12, 13)
    assert not result.nuts.blocker_aware.hero_is_nuts
    assert result.nuts.range.hero_is_nuts
    assert result.nuts.range.best_rank == (6, 12, 13)
    assert result.range_comparison.combinations_evaluated == 2
    assert result.range_comparison.current_ahead_weight == pytest.approx(2 / 3)
    assert result.range_comparison.current_tied_weight == pytest.approx(1 / 3)
    assert result.range_comparison.current_behind_weight == 0
    assert result.opponents.beating_combinations_count > 0


def test_current_comparison_weights_and_beating_examples_are_complete_but_bounded():
    kwargs = {
        "hero": ["Kh", "Kd"],
        "board": "2h 4s 7d 9c Js".split(),
        "opponent_range": {"AhAd": 2, "JhJd": 1, "3c5c": 1, "AcQc": 1},
    }
    limited = analyze_hand_strength(**kwargs, max_beating_combos=1)
    full = analyze_hand_strength(**kwargs, max_beating_combos=1326)
    comparison = limited.range_comparison
    assert comparison.combinations_evaluated == 4
    assert comparison.ahead_combinations == 2
    assert comparison.tied_combinations == 0
    assert comparison.beating_combinations_count == 2
    assert comparison.current_ahead_weight == pytest.approx(0.4)
    assert comparison.current_tied_weight == 0
    assert comparison.current_behind_weight == pytest.approx(0.6)
    assert comparison.beating_combinations_truncated
    assert len(comparison.beating_combinations) == 1
    assert set(comparison.beating_combinations[0].cards) == {"Ah", "Ad"}
    assert comparison.beating_combinations[0].normalized_weight == pytest.approx(0.4)
    assert full.range_comparison.beating_combinations_count == comparison.beating_combinations_count
    assert full.range_comparison.current_behind_weight == comparison.current_behind_weight
    assert not full.range_comparison.beating_combinations_truncated
    assert len(full.range_comparison.beating_combinations) == 2
    known = set(kwargs["hero"] + kwargs["board"])
    for example in full.opponents.beating_combinations:
        assert not known.intersection(example.cards)
        assert example.rank > full.current_rank
    hidden = analyze_hand_strength(**kwargs, max_beating_combos=0)
    assert hidden.range_comparison.beating_combinations == ()
    assert hidden.range_comparison.beating_combinations_count == 2
    assert hidden.range_comparison.beating_combinations_truncated


def test_quads_board_kicker_has_exact_current_tie_count():
    result = analyze_hand_strength(["Kc", "Qc"], "As Ad Ah Ac 2d".split())
    assert result.current_rank == (7, 14, 13)
    # Three remaining kings: 3*42 hands with one king, C(3,2) with two.
    assert result.opponents.tied_combinations == 129
    assert result.opponents.ahead_combinations == 861
    assert result.opponents.current_tied_weight == pytest.approx(129 / 990)
    assert result.opponents.current_ahead_weight == pytest.approx(861 / 990)
    assert result.opponents.current_behind_weight == 0


def test_flop_is_exhaustively_enumerated_and_output_is_reproducible_json():
    first = analyze_hand_strength(["aS", "kD"], ["2c", "3d", "4h"], max_beating_combos=3)
    second = analyze_hand_strength(["As", "Kd"], ["2c", "3d", "4h"], max_beating_combos=3)
    assert first == second
    assert first.street == "flop"
    assert first.nuts.theoretical.combinations_evaluated == 1176
    assert first.opponents.combinations_evaluated == 1081
    assert sum(
        (
            first.opponents.current_ahead_weight,
            first.opponents.current_tied_weight,
            first.opponents.current_behind_weight,
        )
    ) == pytest.approx(1)
    output = first.to_dict()
    assert json.loads(json.dumps(output, allow_nan=False)) == output
    assert output["hero"] == ["As", "Kd"]
    with pytest.raises(FrozenInstanceError):
        first.street = "river"


def test_duplicate_range_combo_spellings_merge_without_overflow():
    result = analyze_hand_strength(
        ["Qh", "2h"],
        "Qs Qd Kc Ks 3c".split(),
        opponent_range={("2c", "2d"): 1e308, ("2d", "2c"): 1e308, "KhKd": 1e308},
    )
    assert result.range_comparison.combinations_evaluated == 2
    assert result.range_comparison.current_ahead_weight == pytest.approx(2 / 3)
    assert result.range_comparison.current_behind_weight == pytest.approx(1 / 3)


def test_tiny_positive_range_weight_cannot_create_false_range_nuts():
    result = analyze_hand_strength(
        ["Kh", "Kd"],
        "2h 4s 7d 9c Js".split(),
        opponent_range={"AhAd": 1e-308, "3c5c": 1e308},
    )
    assert not result.nuts.range.hero_is_nuts
    assert result.range_comparison.combinations_evaluated == 2
    assert result.range_comparison.beating_combinations_count == 1
    assert result.range_comparison.normalization_underflow_combinations == 1
    assert set(result.range_comparison.beating_combinations[0].cards) == {"Ah", "Ad"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hero": ["As"]},
        {"hero": ["As", "as"]},
        {"hero": ["As", "Kd"], "board": ["2c", "3h"]},
        {"hero": ["As", "Kd"], "board": ["As", "2c", "3h"]},
        {"hero": ["As", "Kd"], "board": ["2c", "3h", "4z"]},
        {"hero": ["As", "Kd"], "max_beating_combos": -1},
        {"hero": ["As", "Kd"], "max_beating_combos": 1327},
        {"hero": ["As", "Kd"], "max_beating_combos": True},
        {"hero": ["As", "Kd"], "opponent_range": {}},
        {"hero": ["As", "Kd"], "opponent_range": {"AsAh": 1}},
        {"hero": ["As", "Kd"], "opponent_range": {"AhAc": -1}},
        {"hero": ["As", "Kd"], "opponent_range": {"AhAc": float("nan")}},
        {"hero": ["As", "Kd"], "opponent_range": {"AhAc": float("inf")}},
        {"hero": ["As", "Kd"], "opponent_range": {"AhAc": True}},
        {"hero": ["As", "Kd"], "opponent_range": {"AhAh": 1}},
        {"hero": ["As", "Kd"], "opponent_range": {"AA": 1}},
    ],
)
def test_invalid_cards_ranges_and_output_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        analyze_hand_strength(**kwargs)
