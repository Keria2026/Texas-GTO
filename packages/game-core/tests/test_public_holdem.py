"""Public replay matches the full rules without accessing hidden private cards."""

from dataclasses import fields, replace
from random import Random

import pytest

from texas_game_core.holdem import HoldemGame
from texas_game_core.public_holdem import HoldemRules, PublicHoldemGame, PublicHoldemState


def rules(stacks=(100, 100), *, button=0, straddle=None, small=1, big=2):
    return HoldemRules(
        stacks,
        small_blind=small,
        big_blind=big,
        straddle=straddle,
        button=button,
    )


def shared_equal(full_game, full, public_game, public):
    public_game.validate_state(public)
    assert not hasattr(public, "hole_cards")
    assert not hasattr(public, "payouts")
    for field in fields(PublicHoldemState):
        if field.name in ("awaiting_settlement", "stacks"):
            continue
        assert getattr(public, field.name) == getattr(full, field.name), field.name
    assert public.stacks == (
        tuple(stack - payout for stack, payout in zip(full.stacks, full.payouts, strict=True))
        if full.terminal
        else full.stacks
    )
    assert public.awaiting_settlement == full.terminal
    assert sum(public.stacks) + public.pot == sum(public_game.initial_stacks)
    assert public_game.actor(public) == full_game.actor(full)
    if not full.pending_deal:
        assert public_game.legal_actions(public) == full_game.legal_actions(full)
    else:
        assert public_game.legal_actions(public) == ()


@pytest.mark.parametrize("button,seed", [(0, 1), (3, 7), (7, 42), (0, 98), (3, 123)])
def test_eight_player_oracle_trajectory_redacts_private_cards_at_every_node(button, seed):
    # Exact mushroom fees are external: the small blind has 760, others 780 available.
    stacks = tuple(760 if i == (button + 1) % 8 else 780 for i in range(8))
    profile = rules(stacks, button=button, straddle=8, small=2, big=4)
    full_game, public_game = profile.game(), PublicHoldemGame(profile)
    full, public, rng = full_game.initial_state(), public_game.initial_state(), Random(seed)
    while full.pending_deal and full.pending_deal[0] >= 0:
        action, _ = full_game.sample_chance_outcome(full, rng)
        full = full_game.transition(full, action)
    shared_equal(full_game, full, public_game, public)
    for _ in range(1000):
        actor = full_game.actor(full)
        if actor is None:
            break
        if actor == -1:
            action, _ = full_game.sample_chance_outcome(full, rng)
            full = full_game.transition(full, action)
            public = public_game.transition(public, action)
        else:
            action = rng.choice(full_game.legal_actions(full))
            full = full_game.transition(full, action)
            recorded = full.history[-1]
            public = public_game.observe_action(
                public,
                player=actor,
                action=action,
                amount_delta=recorded.amount_delta,
                amount_to=recorded.amount_to,
            )
        shared_equal(full_game, full, public_game, public)
    else:
        pytest.fail("Complete trajectory did not terminate")
    assert public.awaiting_settlement
    with pytest.raises(ValueError, match="unknown"):
        public_game.terminal_payoffs(public)


def test_hidden_private_cards_change_showdown_winner_but_never_public_replay():
    profile = rules()
    board = ("2c", "3d", "4h", "7s", "9c")
    private = ((("As", "Ah"), ("Ks", "Kh")), (("Ks", "Kh"), ("As", "Ah")))
    results, payouts = [], []
    for hands in private:
        full_game, public_game = profile.game(), PublicHoldemGame(profile)
        full, public = full_game.initial_state(), public_game.initial_state()
        order = tuple((profile.button + 1 + i) % 2 for i in range(2))
        for index in (0, 1):
            for player in order:
                full = full_game.transition(full, "deal:" + hands[player][index])
        for actor, action in ((0, "raise_to:100"), (1, "call")):
            full = full_game.transition(full, action)
            public = public_game.observe_action(
                public,
                player=actor,
                action=action,
                amount_delta=full.history[-1].amount_delta,
            )
        public = public_game.observe_board(public, board)
        for card in board:
            full = full_game.transition(full, "deal:" + card)
        shared_equal(full_game, full, public_game, public)
        results.append(public)
        payouts.append(full.payouts)
    assert payouts == [(200, 0), (0, 200)]
    assert results[0] == results[1]
    assert results[0].stacks == (0, 0) and results[0].pot == 200


def test_short_all_in_does_not_reopen_and_cumulative_short_all_ins_can_reopen():
    game = PublicHoldemGame(rules((100, 15, 100)))
    state = game.initial_state()
    for action in ("raise_to:10", "raise_to:15", "call"):
        state = game.transition(state, action)
    assert game.actor(state) == 0 and game.legal_actions(state) == ("fold", "call")
    with pytest.raises(ValueError, match="reopened"):
        game.transition(state, "raise_to:23")
    state = game.transition(state, "call")
    assert state.pending_deal == (-1, -1, -1)
    assert state.contributions == (15, 15, 15)

    game = PublicHoldemGame(rules((15, 100, 18, 100, 100)))
    state = game.initial_state()
    for action in ("raise_to:10", "call", "raise_to:15", "call", "raise_to:18"):
        state = game.transition(state, action)
    assert game.actor(state) == 3 and "raise_to:26" in game.legal_actions(state)
    state = game.transition(state, "call")
    assert game.actor(state) == 4 and "raise_to:26" in game.legal_actions(state)
    state = game.transition(state, "call")
    assert game.actor(state) == 1 and game.legal_actions(state) == ("fold", "call")


def test_explicit_debit_is_checked_before_uncalled_return_not_against_net_stack_change():
    game = PublicHoldemGame(rules((100, 20)))
    state = game.observe_action(
        game.initial_state(),
        player=0,
        action="raise_to:100",
        amount_delta=99,
        amount_to=100,
    )
    # The last caller pays 18; the same transition returns 80 to the earlier raiser.
    following = game.observe_action(state, player=1, action="call", amount_delta=18, amount_to=20)
    assert following.stacks == (80, 0) and following.returned == (80, 0)
    assert following.contributions == (20, 20)
    with pytest.raises(ValueError, match="amount"):
        game.observe_action(state, player=1, action="call", amount_delta=17)
    assert state.stacks == (0, 18)  # Rejected observations do not mutate the input.


def test_eight_player_blind_walk_waits_for_settlement_and_refunds_uncalled_amount():
    game = PublicHoldemGame(rules((800,) * 8, straddle=8, small=2, big=4))
    state = game.initial_state()
    for _ in range(7):
        state = game.observe_action(state, player=game.actor(state), action="fold", amount_delta=0)
    assert state.awaiting_settlement and state.board == ()
    assert state.returned[3] == 4 and state.stacks[3] == 796
    assert state.pot == 10 and sum(state.stacks) + state.pot == 6400
    assert game.actor(state) is None and game.legal_actions(state) == ()
    with pytest.raises(ValueError, match="terminal"):
        game.transition(state, "check")


def test_eight_player_side_pots_are_not_invented_before_observed_showdown():
    profile = rules((8, 12, 16, 20, 36, 24, 28, 32), straddle=8, small=2, big=4)
    game = PublicHoldemGame(profile)
    state = game.transition(game.initial_state(), "raise_to:36")
    for _ in range(7):
        state = game.transition(state, "call")
    state = game.observe_board(state, ("As", "Ks", "Qs", "Js", "Ts"))
    assert state.awaiting_settlement and state.returned[4] == 4
    assert state.contributions == (8, 12, 16, 20, 32, 24, 28, 32)
    assert state.stacks == (0, 0, 0, 0, 4, 0, 0, 0)
    assert state.pot == 172
    assert not hasattr(state, "side_pots")


def test_turn_order_amounts_off_tree_sizes_and_canonical_action_format():
    game = PublicHoldemGame(rules())
    state = game.initial_state()
    with pytest.raises(ValueError, match="actor"):
        game.observe_action(state, player=1, action="call", amount_delta=1)
    with pytest.raises(ValueError, match="amount"):
        game.observe_action(state, player=0, action="call", amount_delta=1, amount_to=1)
    changed = game.observe_action(
        state,
        player=0,
        action="raise_to:5",
        amount_delta=4,
        amount_to=5,
    )
    assert changed.history[-1].in_action_abstraction is False
    assert changed.history[-1].action == "raise_to:5"
    assert game.minimum_raise_to(changed) == 8
    for action in ("raise_to:05", "raise_to:3", "raise_to:101", "check"):
        with pytest.raises(ValueError):
            game.transition(state, action)


def test_board_must_be_observed_in_order_and_cannot_skip_player_actions():
    game = PublicHoldemGame(rules())
    original = game.initial_state()
    with pytest.raises(ValueError, match="before"):
        game.observe_board(original, ("As", "Kd", "Qc"))
    state = game.transition(game.transition(original, "call"), "check")
    with pytest.raises(ValueError, match="actor"):
        game.observe_action(state, player=-1, action="deal:As", amount_delta=0)
    with pytest.raises(ValueError, match="before"):
        game.observe_board(state, ("As", "Kd", "Qc", "2h"))
    assert state.board == ()
    flop = game.observe_board(state, ("As", "Kd", "Qc"))
    assert flop.board == ("As", "Kd", "Qc") and flop.pending_deal == ()
    assert game.observe_board(flop, flop.board) == flop
    for cards in (("As", "As", "Qc"), ("as", "Kd", "Qc"), (), ("As",), ("As", "Kd")):
        with pytest.raises(ValueError):
            game.observe_board(flop, cards)
    for name in ("chance_outcomes", "sample_chance_outcome"):
        with pytest.raises(ValueError, match="observed"):
            getattr(game, name)(state, *([Random(0)] if name == "sample_chance_outcome" else []))


@pytest.mark.parametrize(
    "change",
    [
        {"stacks": (99.0, 98)},
        {"contributions": (True, 2)},
        {"current_bet": 2.0},
        {"last_full_raise": True},
        {"street": False},
        {"pending": (False, 1)},
        {"to_act": False},
        {"acted_to": (None, False)},
        {"returned": (0.0, 0)},
        {"awaiting_settlement": 0},
        {"folded": (0, False)},
        {"board": []},
        {"pending_deal": (False,)},
        {"history": []},
        {"stacks": (100, 98)},
        {"pending": (0, 0)},
        {"pending": (1, 0)},
        {"board": ("As",)},
        {"rules_version": "wrong"},
    ],
)
def test_public_state_rejects_noncanonical_types_and_accounting(change):
    game = PublicHoldemGame(rules())
    with pytest.raises(ValueError):
        game.validate_state(replace(game.initial_state(), **change))


@pytest.mark.parametrize(
    "player,delta,total", [(True, 1, 2), (0.0, 1, 2), (0, True, 2), (0, 1.0, 2), (0, 1, 2.0)]
)
def test_observed_amount_and_seat_types_do_not_use_python_numeric_equality(player, delta, total):
    game = PublicHoldemGame(rules())
    with pytest.raises(ValueError):
        game.observe_action(
            game.initial_state(),
            player=player,
            action="call",
            amount_delta=delta,
            amount_to=total,
        )


def test_history_types_are_checked_and_full_engine_validation_remains_strict():
    game = PublicHoldemGame(rules())
    state = game.transition(game.initial_state(), "call")
    for change in (
        {"player": False},
        {"amount_delta": True},
        {"amount_to": 2.0},
        {"amount_to": 999},
        {"in_action_abstraction": 1},
    ):
        malformed = replace(state.history[0], **change)
        with pytest.raises(ValueError):
            game.validate_state(replace(state, history=(malformed,)))
    full = HoldemGame((100, 100))
    dealt = full.deal_to_decision(full.initial_state(), rng=Random(0))
    with pytest.raises(ValueError, match="private deal count"):
        full.validate_state(replace(dealt, hole_cards=((), ())))
    with pytest.raises(ValueError, match="PublicHoldemState"):
        game.validate_state(dealt)
