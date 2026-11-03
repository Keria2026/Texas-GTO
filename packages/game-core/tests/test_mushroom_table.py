from random import Random

import pytest

from texas_game_core.mushroom_table import MushroomTable


def play(table, actions, deck=None):
    rng = Random(42)
    table.deal_to_decision(rng=rng, deck=deck)
    for action in actions:
        table.transition(action)
        table.deal_to_decision(rng=rng, deck=deck)


def four_player_deck():
    hands = (("Qc", "Qd"), ("Jc", "Jd"), ("Ac", "Ad"), ("Kc", "Kd"))
    return tuple(hands[seat][card] for card in (0, 1) for seat in (1, 2, 3, 0)) + (
        "2c",
        "3d",
        "7h",
        "9s",
        "Tc",
    )


def test_fee_ineligible_players_are_removed_without_charge_and_blinds_advance():
    table = MushroomTable((800, 40, 39, 800, 800, 800, 800, 800))
    table.begin_hand()
    assert table.participants == (0, 3, 4, 5, 6, 7)
    assert table.stacks[1:3] == (40, 39)
    assert table.participants[table.hand.game.small_blind_player] == 3
    assert table.participants[table.hand.game.big_blind_player] == 4
    assert table.participants[table.hand.game.straddle_player] == 5
    assert table.pool == 140
    assert "1" not in dict(table.ledger.snapshot().outstanding_contributions)
    with pytest.raises(ValueError, match="open hand"):
        table.begin_hand()
    assert table.pool == 140
    table.validate()


def test_four_to_three_ends_before_another_fee_and_refunds_actual_contributions():
    table = MushroomTable((25, 800, 800, 800))
    table.begin_hand()
    play(table, ["call", "call", "call", "check"] + ["check"] * 9, four_player_deck())
    assert table.hand.state.terminal
    assert table.stacks[0] == 0
    assert table.pool == 100
    before = table.stacks
    assert table.begin_hand() is None
    assert table.ended
    assert table.pool == 0
    assert table.hand_number == 1
    assert table.stacks == tuple(before[i] + fee for i, fee in enumerate((20, 40, 20, 20)))
    assert table.stacks[0] == 20
    assert sum(table.stacks) == 2425
    with pytest.raises(ValueError, match="ended"):
        table.begin_hand()
    table.validate()


def test_eight_to_seven_keeps_the_session_active_and_does_not_reset_balances():
    table = MushroomTable((25,) + (800,) * 7)
    table.begin_hand()
    hands = (
        ("Qc", "Qd"),
        ("Jc", "Jd"),
        ("Ac", "Ad"),
        ("Kc", "Kd"),
        ("8c", "8d"),
        ("6c", "6d"),
        ("5c", "5d"),
        ("4c", "4d"),
    )
    deck = tuple(hands[seat][card] for card in (0, 1) for seat in (1, 2, 3, 4, 5, 6, 7, 0)) + (
        "2c",
        "3h",
        "7s",
        "9s",
        "Tc",
    )
    play(table, ["call"] * 7 + ["check"] * 22, deck)
    assert table.hand.state.terminal
    assert table.stacks[0] == 0
    before = table.stacks
    assert table.pool == 180
    table.begin_hand()
    assert table.participants == (1, 2, 3, 4, 5, 6, 7)
    assert not table.ended
    assert table.stacks[0] == 0
    assert table.pool == 340
    assert table.hand.starting_stacks == before[1:]
    table.validate()


def test_split_main_pot_records_no_award_and_session_refunds_each_fee_exactly():
    stacks = (28, 48, 28, 28)
    table = MushroomTable(stacks)
    table.begin_hand()
    deck = ("2c", "3c", "4c", "5c", "6d", "7d", "8d", "9d", "As", "Ks", "Qs", "Js", "Ts")
    play(table, ["call"] * 3, deck)
    assert table.hand.state.terminal
    assert table.pool == 100
    assert table.hand.reveal_required == ()
    table.begin_hand()
    assert table.ended
    assert table.stacks == stacks
    assert table.pool == 0


def test_top_up_between_hands_rejoins_the_existing_seat_and_preserves_accounting():
    table = MushroomTable((20,) + (800,) * 7)
    table.top_up(0, 780)
    table.begin_hand()
    assert table.participants == tuple(range(8))
    assert table.funding_total == 6400
    assert table.pool == 180
    with pytest.raises(ValueError, match="between hands"):
        table.top_up(0, 10)
    table.validate()


def test_top_up_minimum_is_the_resulting_400_points_and_rejection_preserves_money():
    table = MushroomTable((24,) + (800,) * 7)
    before = table.stacks, table.funding_total, table.ledger.snapshot()
    with pytest.raises(ValueError, match="at least 400"):
        table.top_up(0, 375)
    assert (table.stacks, table.funding_total, table.ledger.snapshot()) == before
    table.top_up(0, 376)
    assert table.stacks[0] == 400
    assert table.funding_total == before[1] + 376
    table.validate()


def test_observed_training_boundary_skips_unfunded_straddle_without_charging_it():
    # Actual iteration-8 branch: seat 4 has 24, leaving only 4 after the fee.
    stacks = (644, 624, 776, 772, 24, 338, 780, 2262)
    table = MushroomTable(stacks, button=1)
    table.begin_hand()
    assert table.participants == (0, 1, 2, 3, 5, 6, 7)
    assert table.waiting_for_top_up == (4,)
    assert table.stacks[4] == 24
    assert table.participants[table.hand.game.straddle_player] == 5
    assert table.pool == 160
    assert "4" not in dict(table.ledger.snapshot().outstanding_contributions)
    play(table, ["fold"] * 6)
    table.begin_hand()
    assert table.waiting_for_top_up == (4,) and 4 not in table.participants
    assert table.stacks[4] == 24
    play(table, ["fold"] * 6)
    table.top_up(4, 376)
    table.begin_hand()
    assert table.waiting_for_top_up == ()
    assert 4 in table.participants
    assert table.hand.starting_stacks[4] == 400
    assert table.funding_total == sum(stacks) + 376
    table.validate()


def test_straddle_seat_is_rechecked_after_each_skipped_player_and_four_to_three_ends():
    table = MushroomTable((800, 800, 800, 24, 27, 28, 800, 800))
    table.begin_hand()
    assert table.waiting_for_top_up == (3, 4)
    assert table.participants[table.hand.game.straddle_player] == 5
    assert table.stacks[3:6] == (24, 27, 0)
    assert table.pool == 140
    table.validate()
    short = MushroomTable((800, 800, 800, 24))
    assert short.begin_hand() is None
    assert short.ended and short.pool == 0 and short.hand_number == 0
    assert short.stacks == (800, 800, 800, 24)


def test_sole_small_blind_award_clears_old_fee_ownership_before_the_next_hand():
    table = MushroomTable()
    table.begin_hand()
    play(table, ["fold"] * 5 + ["raise_to:16", "fold", "fold"])
    assert table.pool == 0
    assert table.stacks[1] == 952
    assert table.ledger.snapshot().outstanding_contributions == ()
    table.deal_to_decision()
    assert table.stacks[1] == 952
    assert table.button == 1
    table.begin_hand()
    assert table.pool == 180
    assert dict(table.ledger.snapshot().outstanding_contributions)["1"] == 20
    table.validate()
