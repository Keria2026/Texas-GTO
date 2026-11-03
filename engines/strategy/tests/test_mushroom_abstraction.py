"""Context reuse must preserve real wagers, money and private-information boundaries."""

from dataclasses import replace
from random import Random

import pytest

from texas_game_core.cards import DECK
from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer
from texas_strategy.mushroom_abstraction import CHIP_EDGES, MushroomContextGame, chip_bucket
from texas_strategy.policy import PolicyEntry, TabularPolicy
from texas_strategy.search import action_semantics


def game(stacks=(800,) * 8, *, button=0, horizon=2):
    return MushroomContextGame(MushroomSessionGame(stacks, button=button, horizon=horizon))


def deal(model, state=None, seed=42):
    state = model.initial_state() if state is None else state
    rng = Random(seed)
    while model.actor(state) == -1:
        action, _ = model.sample_chance_outcome(state, rng)
        state = model.transition(state, action)
    return state


def test_chip_bins_are_explicit_monotone_and_do_not_merge_tiny_forced_bets():
    assert tuple(sorted(set(CHIP_EDGES))) == CHIP_EDGES
    assert [chip_bucket(i) for i in range(9)] == list(range(9))
    assert chip_bucket(780) == chip_bucket(781)
    assert chip_bucket(400) != chip_bucket(800)
    for amount in range(6400):
        assert chip_bucket(amount) <= chip_bucket(amount + 1)
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValueError):
            chip_bucket(invalid)


@pytest.mark.parametrize("players,button", [(4, 0), (4, 2), (8, 0), (8, 5)])
def test_abstract_actions_replay_the_exact_same_rules_and_final_net_points(players, button):
    model = game((800,) * players, button=button)
    state, exact = model.initial_state(), model.base_game.initial_state()
    rng = Random(17)
    while (actor := model.actor(state)) is not None:
        if actor == -1:
            action, _ = model.sample_chance_outcome(state, rng)
            concrete = action
        else:
            mapping = model.action_map(state)
            assert tuple(mapping.values()) == model.base_game.legal_actions(exact)
            action = rng.choice(tuple(mapping))
            concrete = mapping[action]
        state = model.transition(state, action)
        exact = model.base_game.transition(exact, concrete)
        assert model.public_key(state) == model.base_game.public_key(exact)
    expected = model.base_game.terminal_payoffs(exact)
    assert model.terminal_payoffs(state) == tuple(
        expected[(i + button) % players] for i in range(players)
    )
    assert sum(model.terminal_payoffs(state)) == 0
    assert sum(state.underlying.table.stacks) == 800 * players


def test_one_policy_context_matches_nearby_stacks_but_uses_their_actual_wagers():
    first, second = game(), game((801,) * 8)
    a, b = deal(first), deal(second)
    actor = first.actor(a)
    assert actor == second.actor(b)
    assert first.rules_version == second.rules_version
    assert first.information_key(a, actor) == second.information_key(b, actor)
    actions = first.legal_actions(a)
    assert actions == second.legal_actions(b)
    policy = TabularPolicy(
        {
            (actor, first.information_key(a, actor)): PolicyEntry(
                actions, (1 / len(actions),) * len(actions)
            )
        },
        missing="error",
    )
    assert policy.contains(second, b)
    assert first.concrete_action(a, "all_in") == "raise_to:780"
    assert second.concrete_action(b, "all_in") == "raise_to:781"
    assert action_semantics("all_in") == "raise"
    changed = second.transition(b, "all_in")
    assert changed.underlying.table.stacks[actor] == 0
    distant = game((400,) * 8)
    assert not policy.contains(distant, deal(distant))


def test_rotating_the_table_reuses_keys_and_preserves_seat_aligned_net_points():
    stacks = (800, 500, 600, 1000, 750, 650, 900, 1100)
    rotation = 3
    rotated = stacks[-rotation:] + stacks[:-rotation]
    first, second = game(stacks), game(rotated, button=rotation)
    a, b = first.initial_state(), second.initial_state()
    rng = Random(82)
    assert first.rules_version == second.rules_version
    while (actor := first.actor(a)) is not None:
        assert actor == second.actor(b)
        if actor == -1:
            action, _ = first.sample_chance_outcome(a, rng)
        else:
            assert first.legal_actions(a) == second.legal_actions(b)
            assert first.information_key(a, actor) == second.information_key(b, actor)
            action = rng.choice(first.legal_actions(a))
        a, b = first.transition(a, action), second.transition(b, action)
    assert first.terminal_payoffs(a) == second.terminal_payoffs(b)


def test_opponent_private_cards_are_not_part_of_the_context():
    model, state = game(), deal(game())
    actor = model.actor(state)
    table = state.underlying.table.fork()
    hole = list(table.hand.state.hole_cards)
    used = {card for cards in hole for card in cards}
    other = (actor + 1) % 8
    hole[other] = tuple(card for card in DECK if card not in used)[:2]
    table.hand = replace(table.hand, state=replace(table.hand.state, hole_cards=tuple(hole)))
    changed = replace(state, underlying=replace(state.underlying, table=table))
    assert model.information_key(changed, actor) == model.information_key(state, actor)


def test_only_completed_hand_recall_is_dropped_and_current_action_order_is_kept():
    model = game()
    state = deal(model)
    while state.underlying.table.hand_number == 1:
        actions = model.legal_actions(state)
        state = model.transition(state, "check" if "check" in actions else "call")
        if state.underlying.table.hand_number == 1:
            state = deal(model, state)
    assert state.actions == ()
    # A search cutoff can precede private dealing in the new hand.
    assert model.information_key(state, 0)
    state = deal(model, state)
    actor = model.actor(state)
    changed = replace(
        state,
        underlying=replace(state.underlying, public_history=(), private_history=((),) * 8),
    )
    assert model.information_key(changed, actor) == model.information_key(state, actor)
    assert model.public_key(changed) != model.public_key(state)
    altered_actions = replace(state, actions=((0, actor, "call"),))
    assert model.information_key(altered_actions, actor) != model.information_key(state, actor)


def test_mushroom_contribution_ownership_and_waiting_seats_remain_in_the_key():
    model = game()
    state = deal(model)
    actor = model.actor(state)
    table = state.underlying.table.fork()
    ledger = table.ledger.snapshot()
    fees = dict(ledger.outstanding_contributions)
    fees["0"], fees["1"] = fees["1"], fees["0"]
    table.ledger._state = replace(ledger, outstanding_contributions=tuple(sorted(fees.items())))
    changed = replace(state, underlying=replace(state.underlying, table=table))
    assert model.information_key(changed, actor) != model.information_key(state, actor)
    table = state.underlying.table.fork()
    table.waiting_for_top_up = (0,)
    changed = replace(state, underlying=replace(state.underlying, table=table))
    assert model.information_key(changed, actor) != model.information_key(state, actor)


def test_off_tree_raise_is_recorded_without_rounding_it_to_a_trained_action():
    model = game()
    state = deal(model)
    assert "raise_to:17" not in model.action_map(state).values()
    changed = model.transition(state, "raise_to:17")
    assert changed.actions[-1][2] == "raise_to:17"
    assert changed.underlying.table.hand.state.current_bet == 17
    with pytest.raises(ValueError):
        model.transition(state, "raise_to:15")


def test_card_suit_symmetry_preserves_keys_but_distinct_hole_ranks_do_not():
    model = game()
    state = deal(model)
    actor = model.actor(state)
    table = state.underlying.table.fork()
    mapping = dict(zip("cdhs", "dhsc", strict=True))
    hole = tuple(
        tuple(card[0] + mapping[card[1]] for card in cards) for cards in table.hand.state.hole_cards
    )
    table.hand = replace(table.hand, state=replace(table.hand.state, hole_cards=hole))
    changed = replace(state, underlying=replace(state.underlying, table=table))
    assert model.information_key(changed, actor) == model.information_key(state, actor)
    assert model.information_key(deal(model, seed=43), actor) != model.information_key(state, actor)


def test_context_checkpoint_resume_is_deterministic_and_cannot_mix_with_exact_history(tmp_path):
    model = game((48,) * 4, horizon=1)
    settings = {"snapshot_interval": 1, "strategy_interval": 10_000}
    trainer = Trainer(model, algorithm="pluribus", seed=42, pluribus=settings)
    trainer.train(2)
    path = tmp_path / "context.json"
    trainer.save_checkpoint(path)
    resumed = Trainer.load_checkpoint(model, path)
    assert resumed.train(2).completed_iterations == 2
    assert trainer.train(2).completed_iterations == 2
    assert resumed._table == trainer._table and resumed._rng.getstate() == trainer._rng.getstate()
    policy_path = tmp_path / "blueprint.json"
    Blueprint.from_trainer(resumed).save(policy_path)
    assert Blueprint.load(policy_path, game=model).metadata["iterations"] == 4
    with pytest.raises(ValueError, match="incompatible"):
        Trainer.load_checkpoint(model.base_game, path)
    with pytest.raises(ValueError, match="incompatible"):
        Blueprint.load(policy_path, game=game((48,) * 4, horizon=2))
    for changed_root in (game((49,) * 4, horizon=1), game((48,) * 4, button=1, horizon=1)):
        assert changed_root.rules_version == model.rules_version
        with pytest.raises(ValueError, match="training context"):
            Trainer.load_checkpoint(changed_root, path)
