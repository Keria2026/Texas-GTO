"""Confirmed WPK ante accounting, including short entries and the straddle exception."""

from random import Random

import pytest

from texas_game_core.holdem import HoldemGame
from texas_game_core.mushroom_game import MushroomSessionGame
from texas_game_core.mushroom_table import MushroomTable
from texas_game_core.public_holdem import HoldemRules, PublicHoldemGame


def test_eight_seat_ante_is_dead_money_and_never_pays_the_call():
    table = MushroomTable(ante=4)
    table.begin_hand()
    table.deal_to_decision(rng=Random(1))
    game, state = table.hand.game, table.hand.state
    assert table.pool == 180
    assert state.pot == 46
    assert sum(table.stacks) == 6174
    assert state.street_bets == (0, 2, 4, 8, 0, 0, 0, 0)
    assert state.contributions == (4, 6, 8, 12, 4, 4, 4, 4)
    assert game.actor(state) == 4
    assert game.amount_to_call(state) == 8
    assert game.minimum_raise_to(state) == 16
    assert "raise_to:35" in game.legal_actions(state)
    table.validate()


def test_short_ante_small_blind_can_win_main_pot_and_mushrooms_but_not_side_pot():
    table = MushroomTable((800, 41, 800, 800), ante=4)
    table.begin_hand()
    hands = (("Qc", "Qd"), ("Ac", "Ad"), ("Kc", "Kd"), ("Jc", "Jd"))
    deck = tuple(hands[p][i] for i in (0, 1) for p in (1, 2, 3, 0)) + ("2c", "3d", "7h", "9s", "Tc")
    table.deal_to_decision(deck=deck)
    assert table.participants == (0, 1, 2, 3)
    assert len(table.hand.state.hole_cards[1]) == 2
    assert table.hand.state.contributions[1] == 1
    assert table.hand.state.street_bets[1] == 0
    for action in ("call", "call", "check") + ("check",) * 9:
        table.transition(action)
        table.deal_to_decision(deck=deck)
        table.validate()
    assert table.hand.state.terminal
    assert [(p.amount, p.winners) for p in table.hand.state.side_pots] == [(4, (1,)), (33, (2,))]
    assert table.hand.award == 100
    assert table.stacks == (768, 104, 801, 768)
    assert table.pool == 0


@pytest.mark.parametrize("stack", [24, 28, 31, 32])
def test_straddle_must_cover_fee_ante_and_eight_or_skip_without_charge(stack):
    table = MushroomTable((800, 41, 800, stack, 800, 800, 24, 800), ante=4)
    table.begin_hand()
    assert (3 in table.participants) == (stack == 32)
    assert table.participants[table.hand.game.straddle_player] == (3 if stack == 32 else 4)
    assert table.stacks[3] == (0 if stack == 32 else stack)
    assert 1 in table.participants and 6 in table.participants
    assert table.stacks[1] == table.stacks[6] == 0
    assert table.pool == (180 if stack == 32 else 160)
    table.validate()


@pytest.mark.parametrize("amount", [799, 800, 8000, 8001])
def test_top_up_bounds_apply_to_added_amount_not_resulting_stack(amount):
    table = MushroomTable((7900,) + (800,) * 7, ante=4, waiting_for_top_up=(0,))
    before = table.stacks, table.funding_total, table.waiting_for_top_up
    if 800 <= amount <= 8000:
        table.top_up(0, amount)
        assert table.stacks[0] == 7900 + amount
        assert table.waiting_for_top_up == ()
    else:
        with pytest.raises(ValueError, match="800 to 8000"):
            table.top_up(0, amount)
        assert (table.stacks, table.funding_total, table.waiting_for_top_up) == before
    table.validate()


@pytest.mark.parametrize("ante", [True, -1, 1.5])
def test_invalid_ante_is_rejected(ante):
    with pytest.raises(ValueError, match="ante"):
        HoldemGame(ante=ante)


def test_ante_profile_is_versioned_and_public_replay_has_identical_forced_money():
    old = MushroomSessionGame()
    new = MushroomSessionGame(ante=4)
    assert old.rules_version.startswith("mushroom-session-horizon-v2:")
    assert new.rules_version.startswith("mushroom-session-horizon-v3:")
    opening = new.initial_state().table.hand
    public = PublicHoldemGame(
        HoldemRules(
            opening.game.initial_stacks, small_blind=2, big_blind=4, straddle=8, button=0, ante=4
        )
    )
    state = public.initial_state()
    assert state.stacks == opening.state.stacks
    assert state.contributions == opening.state.contributions
    assert state.street_bets == opening.state.street_bets
    assert state.pot == 46
    with pytest.raises(ValueError, match="short straddle"):
        HoldemGame((20, 20, 20, 11), straddle=8, ante=4)
