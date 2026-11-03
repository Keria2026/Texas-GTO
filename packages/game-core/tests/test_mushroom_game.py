import json
from dataclasses import replace
from random import Random

import pytest

from texas_game_core.cards import DECK
from texas_game_core.mushroom_game import MushroomSessionGame


def deal(game, state, seed=42):
    rng = Random(seed)
    while game.actor(state) == -1:
        action, probability = game.sample_chance_outcome(state, rng)
        assert probability > 0
        state = game.transition(state, action)
    return state


def walk(game, state):
    for _ in range(len(state.table.participants) - 1):
        state = game.transition(state, "fold")
    return state


def test_eight_seat_cross_hand_pool_and_table_balances_are_in_the_training_state():
    game = MushroomSessionGame()
    initial = game.initial_state()
    state = deal(game, initial)
    assert state.table.pool == 180
    assert sum(state.table.stacks) == 6206
    assert game.actor(state) == 4
    second = walk(game, state)
    assert second.table.hand_number == 2
    assert second.table.pool == 360
    assert game.actor(second) == -1
    assert game.betting_round(second) == 4
    assert initial.table.hand_number == 1
    assert initial.table.ledger.snapshot().completed_hands == ()
    assert state.table.pool == 180
    assert len(second.public_history) == 1
    for player in range(8):
        assert second.private_history[player] == (
            tuple(sorted(state.table.hand.state.hole_cards[player])),
        )
    second.table.validate()


def test_horizon_refunds_actual_fees_without_doubling_costs_and_preserves_payoff_vector():
    game = MushroomSessionGame(horizon=2)
    state = walk(game, deal(game, game.initial_state()))
    state = walk(game, deal(game, state, seed=43))
    assert game.actor(state) is None
    assert state.table.pool == 0
    assert game.terminal_payoffs(state) == (0.0, -2.0, -6.0, 2.0, 6.0, 0.0, 0.0, 0.0)
    assert sum(state.table.stacks) == 6400
    assert game.observation(state, 7)["quality"] == "finite_horizon_research_model"
    assert game.legal_actions(state) == ()
    with pytest.raises(ValueError, match="ended"):
        game.transition(state, "fold")


def test_opponent_private_cards_and_private_recall_never_enter_hero_information():
    game = MushroomSessionGame()
    state = deal(game, game.initial_state())
    hero = game.actor(state)
    opponent = (hero + 1) % 8
    changed_table = state.table.fork()
    holes = list(changed_table.hand.state.hole_cards)
    visible = set(state.table.hand.state.hole_cards[hero])
    holes[opponent] = tuple(card for card in DECK if card not in visible)[:2]
    changed_table.hand = replace(
        changed_table.hand, state=replace(changed_table.hand.state, hole_cards=tuple(holes))
    )
    changed_history = list(state.private_history)
    changed_history[opponent] = (("2c", "3d"),)
    changed = replace(state, table=changed_table, private_history=tuple(changed_history))
    assert game.public_key(changed) == game.public_key(state)
    assert game.information_key(changed, hero) == game.information_key(state, hero)
    second = deal(game, walk(game, state))
    public = game.public_key(second)
    for cards in state.table.hand.state.hole_cards:
        for card in cards:
            assert f'"{card}"' not in public
    private = list(second.private_history)
    private[game.actor(second)] = (("2c", "3d"),)
    assert game.information_key(
        replace(second, private_history=tuple(private)), game.actor(second)
    ) != game.information_key(second, game.actor(second))


def test_same_opaque_action_can_fork_without_mutating_other_counterfactual_branches():
    game = MushroomSessionGame()
    root = deal(game, game.initial_state())
    folded = game.transition(root, "fold")
    called = game.transition(root, "call")
    assert root.table.hand.state.history == ()
    assert folded.table.hand.state.folded[4]
    assert not called.table.hand.state.folded[4]
    assert root.table.stacks[4] == 780
    assert called.table.stacks[4] == 772
    assert game.information_key(folded, game.actor(folded)) != game.information_key(
        called, game.actor(called)
    )


def test_mushroom_prize_changes_training_utility_and_only_reveals_the_awarded_small_blind():
    game = MushroomSessionGame(horizon=1)
    state = deal(game, game.initial_state())
    for action in ["fold"] * 5 + ["raise_to:16", "fold", "fold"]:
        state = game.transition(state, action)
    assert game.terminal_payoffs(state) == (-20.0, 152.0, -24.0, -28.0, -20.0, -20.0, -20.0, -20.0)
    assert sum(game.terminal_payoffs(state)) == 0
    record = json.loads(state.public_history[0])
    assert record["mushroomAward"] == 180
    assert record["revealed"] == [1, list(state.table.hand.state.hole_cards[1])]


def test_training_funding_cannot_be_mutated_between_counterfactual_hands():
    game = MushroomSessionGame()
    state = game.initial_state()
    state.table.funding_total += 1
    with pytest.raises(ValueError, match="external funding"):
        game.actor(state)


def test_waiting_for_top_up_is_observable_and_part_of_the_training_rule_version():
    game = MushroomSessionGame((800, 800, 800, 24, 800, 800, 800, 800))
    state = deal(game, game.initial_state())
    assert state.table.waiting_for_top_up == (3,)
    assert game.observation(state, 3)["heroCards"] == []
    assert game.observation(state, 3)["players"][3] == {
        "seat": 3,
        "stack": 24,
        "participating": False,
        "waitingForTopUp": True,
    }
    assert json.loads(game.public_key(state))["waitingForTopUp"] == [3]
    assert game.rules_version.startswith("mushroom-session-horizon-v2:")
    rules = json.loads(game.rules_version.partition(":")[2])
    assert rules["topUpMinimumResultingStack"] == 400
    assert rules["trainingExternalFunding"] == "none"


@pytest.mark.parametrize("horizon", [0, True, 1.5, 17])
def test_invalid_training_horizon_is_rejected(horizon):
    with pytest.raises(ValueError, match="horizon"):
        MushroomSessionGame(horizon=horizon)
