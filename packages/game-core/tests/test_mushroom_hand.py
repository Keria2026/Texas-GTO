from random import Random

import pytest

from texas_game_core.cards import DECK
from texas_game_core.mushroom import UnsupportedRuleError
from texas_game_core.mushroom_hand import MushroomHand


def play(hand, actions, deck=None):
    rng = Random(42)
    hand = hand.deal_to_decision(rng=rng, deck=deck)
    for action in actions:
        hand = hand.transition(action).deal_to_decision(rng=rng, deck=deck)
        hand.validate()
    return hand


def ordered_deck(hands, board):
    order = tuple(range(1, len(hands))) + (0,)
    return tuple(hands[seat][index] for index in (0, 1) for seat in order) + tuple(board)


def test_eight_seat_fees_precede_blinds_and_straddle_and_conserve_6400_points():
    hand = MushroomHand.start((800,) * 8)
    assert hand.fees == (20, 40, 20, 20, 20, 20, 20, 20)
    assert hand.pool == 180
    assert sum(hand.table_stacks) == 6206
    assert hand.state.pot == 14
    assert hand.game.initial_stacks == (780, 760, 780, 780, 780, 780, 780, 780)
    hand.validate()


def test_small_blind_sole_main_winner_gets_pool_once_and_requires_reveal():
    hand = play(
        MushroomHand.start((800,) * 8, pool=360), ["fold"] * 5 + ["raise_to:16", "fold", "fold"]
    )
    assert hand.state.terminal
    assert hand.award_player == 1
    assert hand.award == 540
    assert hand.pool == 0
    assert hand.reveal_required == (1,)
    assert hand.table_stacks[1] == 1312
    with pytest.raises(ValueError, match="terminal"):
        hand.transition("check")
    assert sum(hand.net_changes()) == 360
    next_hand = hand.next_hand(button=1)
    assert next_hand.starting_stacks == hand.table_stacks
    assert next_hand.pool == 180
    next_hand.validate()


def test_non_small_blind_win_carries_pool_and_actual_stacks_into_next_hand():
    hand = play(MushroomHand.start((800,) * 8), ["fold"] * 7)
    assert hand.state.terminal
    assert hand.award_player is None
    assert hand.pool == 180
    assert hand.reveal_required == ()
    assert sum(hand.net_changes()) == -180
    later = hand.next_hand(button=1)
    assert later.starting_stacks == hand.table_stacks
    assert later.pool == 360
    assert later.starting_stacks != (800,) * 8
    later.validate()


def test_short_small_blind_wins_main_pool_even_if_someone_else_wins_side_pots():
    hands = (("Qc", "Qd"), ("Ac", "Ad"), ("Jc", "Jd"), ("Kc", "Kd"))
    board = ("2c", "3d", "7h", "9s", "Tc")
    hand = play(
        MushroomHand.start((30, 45, 40, 50)),
        ["raise_to:10", "call", "raise_to:20", "call"],
        ordered_deck(hands, board),
    )
    assert hand.state.side_pots[0].winners == (1,)
    assert hand.state.side_pots[1].winners == (3,)
    assert hand.award == 100
    assert hand.table_stacks[1] == 120
    hand.validate()


def test_small_blind_winning_only_side_pots_does_not_get_mushroom_pool():
    hands = (("Ac", "Ad"), ("Kc", "Kd"), ("Jc", "Jd"), ("Qc", "Qd"))
    board = ("2c", "3d", "7h", "9s", "Tc")
    hand = play(
        MushroomHand.start((25, 70, 30, 40)),
        ["call", "raise_to:30", "call", "call"],
        ordered_deck(hands, board),
    )
    assert hand.state.side_pots[0].winners == (0,)
    assert hand.state.side_pots[1].winners == (1,)
    assert hand.award == 0
    assert hand.pool == 100
    assert hand.reveal_required == ()


def test_split_main_pot_carries_pool_even_when_small_blind_is_a_joint_winner():
    board = ("As", "Ks", "Qs", "Js", "Ts")
    cards = tuple(c for c in DECK if c not in board)
    hands = tuple((cards[2 * seat], cards[2 * seat + 1]) for seat in range(4))
    hand = play(
        MushroomHand.start((28, 48, 28, 28)), ["call", "call", "call"], ordered_deck(hands, board)
    )
    assert hand.state.terminal
    assert len(hand.state.side_pots[0].winners) == 4
    assert hand.pool == 100
    assert hand.award == 0
    hand.validate()


@pytest.mark.parametrize("stacks", [(800,) * 7 + (20,), (800, 40, 800, 800), (19,) * 4])
def test_fee_boundary_never_deals_a_player_without_betting_chips(stacks):
    with pytest.raises(UnsupportedRuleError, match="eligibility"):
        MushroomHand.start(stacks)


def test_session_policies_are_not_invented_by_the_hand_accounting_block():
    with pytest.raises(ValueError, match="short straddle"):
        MushroomHand.start((800, 800, 800, 27))
    with pytest.raises(ValueError, match="Settle"):
        MushroomHand.start((800,) * 8).next_hand(button=1)
