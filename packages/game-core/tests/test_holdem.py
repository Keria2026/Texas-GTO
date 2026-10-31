"""规则黄金算例；模拟牌不能混入真实平台事件。"""

import json
import random
from dataclasses import replace

import pytest

from texas_game_core.cards import DECK
from texas_game_core.holdem import HoldemGame
from texas_game_core.interfaces import CHANCE_PLAYER


def deck_for(hands: tuple[tuple[str, str], ...], board: tuple[str, ...], button: int = 0):
    order = tuple((button + offset + 1) % len(hands) for offset in range(len(hands)))
    return tuple(hands[player][index] for index in (0, 1) for player in order) + board


def advance(game, state, action, rng=None):
    return game.deal_to_decision(game.transition(state, action), rng=rng)


def test_heads_up_button_posts_small_blind_and_acts_first_preflop_last_postflop():
    game = HoldemGame((100, 100), button=1)
    state = game.replay([])
    assert state.contributions == (2, 1)
    assert game.actor(state) == 1
    state = advance(game, state, "call")
    assert game.actor(state) == 0
    state = advance(game, state, "check")
    assert state.street_name == "flop"
    assert len(state.board) == 3
    assert state.street_bets == (0, 0)
    assert game.actor(state) == 0


def test_three_player_order_and_big_blind_option():
    game = HoldemGame((100, 100, 100))
    state = game.replay([])
    assert state.contributions == (0, 1, 2)
    assert game.actor(state) == 0
    state = advance(game, state, "call")
    assert game.actor(state) == 1
    state = advance(game, state, "call")
    assert game.actor(state) == 2
    assert "check" in game.legal_actions(state)
    state = advance(game, state, "check")
    assert state.street_name == "flop"
    assert game.actor(state) == 1


def test_fold_returns_uncalled_blind_and_finishes_without_board():
    game = HoldemGame((20, 20))
    state = game.replay(["fold"])
    assert state.terminal
    assert state.board == ()
    assert state.stacks == (19, 21)
    assert state.returned == (0, 1)
    assert state.payouts == (0, 2)
    assert game.terminal_payoffs(state) == (-1.0, 1.0)
    assert state.side_pots[0].eligible == (1,)
    assert game.legal_actions(state) == ()
    with pytest.raises(ValueError, match="terminal"):
        game.transition(state, "check")


def test_raise_to_means_total_commitment_and_accepts_nonabstract_legal_sizes():
    game = HoldemGame((100, 100))
    state = game.replay([])
    assert "raise_to:5" not in game.legal_actions(state)
    state = game.transition(state, "raise_to:5")
    assert state.street_bets == (5, 2)
    assert state.stacks == (95, 98)
    assert state.history[-1].amount_delta == 4
    assert state.history[-1].amount_to == 5
    assert state.last_full_raise == 3
    assert game.minimum_raise_to(state) == 8
    assert game.amount_to_call(state) == 3
    observation = game.observation(state, 1)
    assert observation["actionAbstraction"]["offTreeActionCount"] == 1
    assert observation["history"][-1]["inActionAbstraction"] is False
    assert observation["betting"]["minimumRaiseTo"] == 8


def test_large_finite_bet_fraction_clamps_to_all_in_without_float_overflow():
    game = HoldemGame((100, 100), bet_fractions=(1e308,))
    assert game.legal_actions(game.replay([])) == ("fold", "call", "raise_to:4", "raise_to:100")


@pytest.mark.parametrize(
    "action",
    [
        "check",
        "raise_to:3",
        "raise_to:101",
        "raise_to:-1",
        "raise_to:05",
        "raise_to:4.0",
        "raise_to:٤",
        "all-in",
    ],
)
def test_illegal_player_actions_rejected(action):
    game = HoldemGame((100, 100))
    with pytest.raises(ValueError):
        game.transition(game.replay([]), action)


def test_short_all_in_does_not_reopen_previously_acted_player():
    game = HoldemGame((100, 15, 100))
    state = game.replay(["raise_to:10", "raise_to:15", "call"])
    assert game.actor(state) == 0
    assert state.last_full_raise == 8
    assert game.legal_actions(state) == ("fold", "call")
    with pytest.raises(ValueError, match="reopened"):
        game.transition(state, "raise_to:23")
    state = advance(game, state, "call")
    assert state.street_name == "flop"
    assert state.contributions == (15, 15, 15)


def test_cumulative_short_all_ins_reopen_only_players_facing_a_full_increment():
    game = HoldemGame((15, 100, 18, 100, 100))
    state = game.replay(["raise_to:10", "call", "raise_to:15", "call", "raise_to:18"])
    assert game.actor(state) == 3
    assert "raise_to:26" in game.legal_actions(state)
    state = advance(game, state, "call")
    assert game.actor(state) == 4
    assert "raise_to:26" in game.legal_actions(state)
    state = advance(game, state, "call")
    assert game.actor(state) == 1
    assert game.legal_actions(state) == ("fold", "call")
    assert state.acted_to[1] == 15


def test_incomplete_opening_all_in_still_requires_full_raise_increment():
    game = HoldemGame((100, 100, 3))
    state = game.replay(["call", "call", "check", "check", "raise_to:1", "call"])
    assert state.street_name == "flop"
    assert game.actor(state) == 1
    assert "raise_to:3" in game.legal_actions(state)
    with pytest.raises(ValueError, match="incomplete"):
        game.transition(state, "raise_to:2")
    state = game.transition(state, "raise_to:3")
    assert game.actor(state) == 0
    assert state.last_full_raise == 2
    assert "raise_to:5" in game.legal_actions(state)


def test_short_big_blind_keeps_full_bring_in_with_two_funded_players():
    game = HoldemGame((20, 20, 1))
    state = game.replay([])
    assert state.current_bet == 2
    assert state.contributions == (0, 1, 1)
    assert game.amount_to_call(state) == 2
    state = advance(game, state, "call")
    assert game.amount_to_call(state) == 1
    state = advance(game, state, "call")
    assert state.contributions == (2, 2, 1)
    assert state.street_name == "flop"


def test_lone_funded_player_does_not_pay_uncontestable_nominal_bring_in():
    game = HoldemGame((20, 1), small_blind=1, big_blind=2)
    state = game.replay([])
    assert state.terminal
    assert state.contributions == (1, 1)
    assert state.history == ()
    assert len(state.board) == 5


def test_all_in_runout_and_uncalled_excess_return():
    game = HoldemGame((100, 20))
    deck = deck_for((("As", "Ah"), ("Ks", "Kh")), ("2c", "3d", "4h", "7s", "9c"))
    state = game.replay(["raise_to:100", "call"], deck=deck)
    assert state.terminal
    assert len(state.board) == 5
    assert state.contributions == (20, 20)
    assert state.returned == (80, 0)
    assert state.payouts == (40, 0)
    assert state.stacks == (120, 0)
    assert game.terminal_payoffs(state) == (20.0, -20.0)


def test_three_player_side_pots_separate_eligibility_and_zero_sum():
    game = HoldemGame((10, 20, 30))
    deck = deck_for(
        (("As", "Ah"), ("Ks", "Kh"), ("Qs", "Qh")),
        ("2c", "3d", "4h", "7s", "9c"),
    )
    state = game.replay(["raise_to:10", "raise_to:20", "call"], deck=deck)
    assert state.terminal
    assert state.contributions == (10, 20, 20)
    assert state.stacks == (30, 20, 10)
    assert [(pot.amount, pot.eligible, pot.winners) for pot in state.side_pots] == [
        (30, (0, 1, 2), (0,)),
        (20, (1, 2), (1,)),
    ]
    assert game.terminal_payoffs(state) == (20.0, 0.0, -20.0)
    assert sum(game.terminal_payoffs(state)) == 0


def test_folded_dead_money_goes_only_to_eligible_tied_players():
    game = HoldemGame((30, 30, 30), button=0)
    # All three invest 4; SB folds to a 3-chip flop bet. The board plays for
    # the remaining seats, and the folded seat's dead money is shared.
    deck = deck_for(
        (("2c", "3d"), ("4h", "5s"), ("6c", "7d")),
        ("As", "Ks", "Qs", "Js", "Ts"),
    )
    state = game.replay(
        [
            "raise_to:4",
            "call",
            "call",
            "check",
            "raise_to:3",
            "call",
            "fold",
            "check",
            "check",
            "check",
            "check",
        ],
        deck=deck,
    )
    assert state.stacks == (32, 26, 32)
    assert state.side_pots[0].eligible == (0, 2)
    assert all(1 not in pot.winners for pot in state.side_pots)


def test_odd_chip_is_awarded_clockwise_left_of_button():
    game = HoldemGame((20, 20, 20), small_blind=1, big_blind=1)
    deck = deck_for(
        (("2c", "3d"), ("4h", "5s"), ("6c", "7d")),
        ("As", "Ks", "Qs", "Js", "Ts"),
    )
    state = game.replay(
        [
            "call",
            "check",
            "check",
            "check",
            "raise_to:1",
            "call",
            "fold",
            "check",
            "check",
            "check",
            "check",
        ],
        deck=deck,
    )
    assert state.contributions == (2, 1, 2)
    assert state.stacks == (20, 19, 21)
    assert state.side_pots[0].awards == ((2, 2), (0, 1))
    assert sum(game.terminal_payoffs(state)) == 0


def test_board_plays_and_equal_stacks_tie_exactly():
    game = HoldemGame((20, 20))
    deck = deck_for((("2c", "3d"), ("4h", "5s")), ("As", "Ks", "Qs", "Js", "Ts"))
    state = game.replay(["raise_to:20", "call"], deck=deck)
    assert state.stacks == (20, 20)
    assert game.terminal_payoffs(state) == (0.0, 0.0)


def test_simulation_information_set_never_contains_opponent_hole_cards():
    game = HoldemGame((20, 20, 20))
    first = game.replay([], deck=("As", "Ks", "Qs", "Ah", "Kh", "Qh"))
    # Seat 0 receives Qs Qh; swap other players' hands without changing any
    # public observation. Both worlds must belong to the same hero infoset.
    second = replace(
        first, hole_cards=(first.hole_cards[0], first.hole_cards[2], first.hole_cards[1])
    )
    assert game.information_key(first, 0) == game.information_key(second, 0)
    assert game.public_key(first) == game.public_key(second)
    assert game.observation(first, 0) == game.observation(second, 0)
    serialized = json.dumps(game.observation(first, 0))
    for card in first.hole_cards[1] + first.hole_cards[2]:
        assert card not in serialized
    assert game.observation(first, 0)["heroCards"] == ["Qs", "Qh"]


def test_private_chance_identity_is_absent_from_public_key():
    game = HoldemGame((20, 20))
    first = game.transition(game.initial_state(), "deal:As")
    second = game.transition(game.initial_state(), "deal:Ks")
    assert game.public_key(first) == game.public_key(second)
    assert game.information_key(first, 0) == game.information_key(second, 0)
    assert game.information_key(first, 1) != game.information_key(second, 1)


def test_chance_probabilities_and_duplicate_cards_rejected():
    game = HoldemGame((20, 20))
    initial = game.initial_state()
    assert game.actor(initial) == CHANCE_PLAYER
    assert len(game.chance_outcomes(initial)) == 52
    assert sum(prob for _, prob in game.chance_outcomes(initial)) == pytest.approx(1)
    second = game.transition(initial, "deal:As")
    assert len(game.chance_outcomes(second)) == 51
    assert "deal:As" not in game.legal_actions(second)
    with pytest.raises(ValueError):
        game.transition(second, "deal:As")
    with pytest.raises(ValueError):
        game.replay([], deck=("As", "As", "Ks", "Kh"))
    with pytest.raises(ValueError, match="exhausted"):
        game.replay([], deck=("As",))


def test_seeded_replay_is_exact_and_explicit_deck_preserves_deal_order():
    game = HoldemGame((20, 20, 20))
    actions = ["call", "call", "check", "check", "check", "check"]
    assert game.replay(actions, seed=481) == game.replay(actions, seed=481)
    assert game.replay(actions, seed=481).hole_cards != game.replay(actions, seed=482).hole_cards
    state = game.replay([], deck=DECK)
    assert state.hole_cards == ((DECK[2], DECK[5]), (DECK[0], DECK[3]), (DECK[1], DECK[4]))


def test_rules_and_seat_validation():
    game = HoldemGame((20, 20))
    with pytest.raises(ValueError, match="configuration"):
        game.actor(HoldemGame((21, 21)).initial_state())
    with pytest.raises(ValueError, match="outside"):
        game.information_key(game.initial_state(), 2)
    with pytest.raises(ValueError, match="settlement"):
        game.terminal_payoffs(game.initial_state())
    for stacks in ((10,), (10,) * 7, (10, 0), (10, 1.5), (True, 10)):
        with pytest.raises(ValueError):
            HoldemGame(stacks)


def test_reconstructed_state_validation_rejects_card_and_chip_contradictions():
    game = HoldemGame((20, 20))
    state = game.replay([])
    with pytest.raises(ValueError, match="duplicate"):
        game.transition(
            replace(state, hole_cards=(state.hole_cards[0], state.hole_cards[0])), "call"
        )
    with pytest.raises(ValueError, match="accounting"):
        game.transition(replace(state, stacks=(20, 20)), "call")
    with pytest.raises(ValueError, match="board"):
        game.transition(replace(state, board=("2c",)), "call")
    with pytest.raises(ValueError, match="vectors"):
        game.transition(replace(state, folded=(False,)), "call")
    all_in = game.replay(["raise_to:20", "call"])
    game.validate_state(all_in)
    fold = game.replay(["raise_to:20", "fold"])
    game.validate_state(fold)


def test_sample_chance_is_a_seeded_legal_action():
    game = HoldemGame((20, 20))
    state = game.initial_state()
    first = game.sample_chance(state, random.Random(10))
    assert first == game.sample_chance(state, random.Random(10))
    assert first in game.legal_actions(state)
    assert game.sample_chance_outcome(state, random.Random(10)) == (first, 1 / 52)
    with pytest.raises(ValueError, match="chance"):
        game.sample_chance(game.replay([]), random.Random(10))


@pytest.mark.parametrize("players", range(2, 7))
def test_random_legal_play_conserves_chips_and_finishes_for_every_supported_table(players):
    rng = random.Random(712 + players)
    for hand in range(35):
        game = HoldemGame(
            tuple(rng.randrange(1, 70) for _ in range(players)), button=hand % players
        )
        state = game.deal_to_decision(game.initial_state(), rng=rng)
        transitions = 0
        while not state.terminal:
            assert sum(state.stacks) + state.pot == sum(game.initial_stacks)
            assert game.actor(state) in game.active_players(state)
            assert all(stack >= 0 for stack in state.stacks)
            state = advance(game, state, rng.choice(game.legal_actions(state)), rng=rng)
            transitions += 1
            assert transitions < 100
        assert sum(state.stacks) == sum(game.initial_stacks)
        game.validate_state(state)
        assert sum(game.terminal_payoffs(state)) == 0
        cards = state.board + tuple(card for cards in state.hole_cards for card in cards)
        assert len(cards) == len(set(cards))
