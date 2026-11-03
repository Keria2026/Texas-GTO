from math import fsum

import pytest

from texas_game_core.games import get_game
from texas_game_core.interfaces import CHANCE_PLAYER
from texas_game_core.kuhn import KuhnPoker, KuhnState
from texas_game_core.leduc import LeducPoker, LeducState


def audit_tree(game):
    infosets = {}
    terminals = 0

    def visit(state):
        nonlocal terminals
        actor = game.actor(state)
        if actor is None:
            terminals += 1
            assert game.legal_actions(state) == ()
            assert fsum(game.terminal_payoffs(state)) == pytest.approx(0)
            return
        if actor == CHANCE_PLAYER:
            outcomes = game.chance_outcomes(state)
            assert fsum(probability for _, probability in outcomes) == pytest.approx(1)
            assert all(probability > 0 for _, probability in outcomes)
            assert tuple(action for action, _ in outcomes) == game.legal_actions(state)
            actions = tuple(action for action, _ in outcomes)
        else:
            actions = game.legal_actions(state)
            key = (actor, game.information_key(state, actor))
            assert infosets.setdefault(key, actions) == actions
            assert actor in game.active_players(state)
        for action in actions:
            visit(game.transition(state, action))

    visit(game.initial_state())
    return terminals, infosets


@pytest.mark.parametrize("players", [2, 3])
def test_kuhn_complete_tree_chance_infosets_and_zero_sum(players):
    game = KuhnPoker(players)
    terminals, infosets = audit_tree(game)
    assert terminals > 0
    if players == 2:
        assert terminals == 30
        assert len(infosets) == 12
    else:
        assert terminals == 312
        assert len(infosets) == 48


def test_known_kuhn_equilibrium_has_classical_minus_one_eighteenth_value():
    """Integrate a published equilibrium family member, independently of training."""
    game = KuhnPoker()

    def expected_value(state):
        player = game.actor(state)
        if player is None:
            return game.terminal_payoffs(state)[0]
        if player == CHANCE_PLAYER:
            return fsum(
                probability * expected_value(game.transition(state, action))
                for action, probability in game.chance_outcomes(state)
            )
        card = state.cards[player]
        # First legal action is check before a bet, fold when facing one.
        if player == 0:
            probabilities = (2 / 3, 1, 0) if not state.history else (1, 1 / 3, 0)
        else:
            probabilities = (2 / 3, 1, 0) if state.history == ("check",) else (1, 2 / 3, 0)
        first = probabilities[card]
        return fsum(
            probability * expected_value(game.transition(state, action))
            for action, probability in zip(
                game.legal_actions(state), (first, 1 - first), strict=True
            )
        )

    assert expected_value(game.initial_state()) == pytest.approx(-1 / 18)


def test_kuhn_standard_payoffs_and_earlier_checkers_respond():
    game = KuhnPoker()
    assert game.terminal_payoffs(game.replay((0, 2), ("check", "check"))) == (-1, 1)
    assert game.terminal_payoffs(game.replay((0, 2), ("bet", "fold"))) == (1, -1)
    assert game.terminal_payoffs(game.replay((0, 2), ("check", "bet", "call"))) == (-2, 2)
    three = KuhnPoker(3)
    state = three.replay((0, 1, 3), ("check", "bet", "fold"))
    assert three.actor(state) == 0
    assert three.active_players(state) == (0, 1)
    terminal = three.transition(state, "call")
    assert three.terminal_payoffs(terminal) == (-2, 3, -1)


def test_kuhn_information_and_public_keys_never_include_other_private_cards():
    game = KuhnPoker(3)
    first = game.replay((0, 1, 2), ("check",))
    second = game.replay((3, 1, 0), ("check",))
    assert game.public_key(first) == game.public_key(second)
    assert game.information_key(first, 1) == game.information_key(second, 1)
    assert game.observation(first, 1) == game.observation(second, 1)
    assert game.information_key(first, 0) != game.information_key(second, 0)


def test_six_player_kuhn_chance_and_showdown():
    game = KuhnPoker(6)
    assert len(game.chance_outcomes(game.initial_state())) == 5040
    state = game.replay((0, 1, 2, 3, 4, 6), ("check",) * 6)
    assert game.terminal_payoffs(state) == (-1, -1, -1, -1, -1, 5)


@pytest.mark.parametrize(
    "state",
    [
        KuhnState((0, 0)),
        KuhnState((True, 1)),
        KuhnState((0, 3)),
        KuhnState((), ("check",)),
        KuhnState((0, 1), ("call",)),
        KuhnState((0, 1), ("bet", "fold", "check")),
    ],
)
def test_invalid_kuhn_states_are_rejected(state):
    with pytest.raises(ValueError):
        KuhnPoker().actor(state)


def test_leduc_complete_tree_has_consistent_information_and_payoffs():
    terminals, infosets = audit_tree(LeducPoker())
    assert terminals == 5520
    assert len(infosets) == 288


def test_leduc_chance_accounts_for_private_blockers_and_copy_indistinguishability():
    game = LeducPoker()
    state = game.replay((0, 2), ("check", "check"))
    outcomes = dict(game.chance_outcomes(state))
    assert outcomes == {"board:1": 0.25, "board:3": 0.25, "board:4": 0.25, "board:5": 0.25}
    first = game.replay((0, 2), ("check",))
    second = game.replay((1, 5), ("check",))
    assert game.information_key(first, 0) == game.information_key(second, 0)
    assert game.public_key(first) == game.public_key(second)


def test_leduc_pair_high_card_split_fold_and_raise_cap():
    game = LeducPoker()
    checks = ("check", "check", "board:1", "check", "check")
    assert game.terminal_payoffs(game.replay((0, 4), checks)) == (1, -1)
    assert game.terminal_payoffs(game.replay((2, 4), checks)) == (-1, 1)
    assert game.terminal_payoffs(game.replay((2, 3), checks)) == (0, 0)
    assert game.terminal_payoffs(game.replay((0, 4), ("bet", "fold"))) == (1, -1)
    raised = game.replay((0, 4), ("bet", "raise"))
    assert game.legal_actions(raised) == ("fold", "call")
    with pytest.raises(ValueError):
        game.transition(raised, "raise")
    river = game.transition(game.transition(raised, "call"), "board:1")
    terminal = game.transition(game.transition(river, "bet"), "call")
    assert game.terminal_payoffs(terminal) == (9, -9)


@pytest.mark.parametrize(
    "state",
    [
        LeducState((0, 0)),
        LeducState((0, True)),
        LeducState((0, 2), 0, (("check", "check"), ())),
        LeducState((0, 2), 1),
        LeducState((0, 2), None, ((), ("bet",))),
        LeducState((0, 2), None, (("bet", "fold", "call"), ())),
    ],
)
def test_invalid_leduc_states_are_rejected(state):
    with pytest.raises(ValueError):
        LeducPoker().actor(state)


def test_factory_and_illegal_actions():
    assert isinstance(get_game("kuhn", 3), KuhnPoker)
    assert isinstance(get_game("leduc"), LeducPoker)
    with pytest.raises(ValueError):
        get_game("holdem")
    with pytest.raises(ValueError):
        get_game("leduc", 3)
    with pytest.raises(ValueError):
        KuhnPoker(True)
    with pytest.raises(ValueError):
        KuhnPoker().transition(KuhnState(), "deal:01,2")
    with pytest.raises(ValueError):
        KuhnPoker().terminal_payoffs(KuhnState())
