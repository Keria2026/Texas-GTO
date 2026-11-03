from collections import Counter, defaultdict
from dataclasses import replace
from itertools import combinations, permutations
from random import Random

import pytest

from texas_game_core.cards import DECK, SUITS
from texas_game_core.holdem import HoldemGame
from texas_strategy.abstraction import (
    SuitCanonicalHoldem,
    canonical_card_trace,
    preflop_class,
)
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer


def dealt(game, cards):
    state = game.initial_state()
    for card in cards:
        assert game.actor(state) == -1
        state = game.transition(state, f"deal:{card}")
    return state


def test_exactly_169_preflop_classes_and_correct_combination_counts():
    labels = Counter(preflop_class(hand) for hand in combinations(DECK, 2))
    canonical = {canonical_card_trace(hand) for hand in combinations(DECK, 2)}
    assert len(labels) == len(canonical) == 169
    assert Counter(labels.values()) == {6: 13, 4: 78, 12: 78}
    assert preflop_class(("Ah", "As")) == "AA"
    assert preflop_class(("Ac", "Kc")) == "AKs"
    assert preflop_class(("Kd", "As")) == "AKo"


def test_all_global_suit_permutations_are_identical_without_losing_flush_blockers():
    hole = ("As", "Kh")
    board = ("Qs", "Js", "2d", "Tc", "4s")
    expected = canonical_card_trace(hole, board)
    for permutation in permutations(SUITS):
        mapping = dict(zip(SUITS, permutation, strict=True))
        remap = lambda cards, mapping=mapping: tuple(c[0] + mapping[c[1]] for c in cards)  # noqa: E731
        assert canonical_card_trace(remap(hole), remap(board)) == expected
    # Same ranks and made category can still carry different flush blockers.
    assert canonical_card_trace(("As", "Kh"), board[:3]) != canonical_card_trace(
        ("Ac", "Kh"), board[:3]
    )


def test_future_board_cannot_change_previous_street_abstraction():
    hole = ("As", "Kd")
    flop = ("2c", "3h", "4s")
    before = canonical_card_trace(hole, flop)
    for turn, river in [("5d", "6s"), ("Ah", "Kh"), ("7c", "8c")]:
        trace = canonical_card_trace(hole, flop + (turn, river))
        assert trace[:2] == before
        assert trace[:3] == canonical_card_trace(hole, flop + (turn,))
    assert canonical_card_trace(hole, flop + ("5d", "6s")) != canonical_card_trace(
        hole, flop + ("6s", "5d")
    )


def test_wrapper_hides_opponent_cards_preserves_actions_and_rule_identity():
    base = HoldemGame(stacks=(8, 8))
    game = SuitCanonicalHoldem(base)
    state = dealt(base, ("As", "Qc", "Kh", "Jd"))
    actor = game.actor(state)
    changed_holes = list(state.hole_cards)
    changed_holes[1 - actor] = ("2c", "3c")
    changed = replace(state, hole_cards=tuple(changed_holes))
    assert game.information_key(state, actor) == game.information_key(changed, actor)
    assert game.legal_actions(state) == base.legal_actions(state)
    assert game.initial_state() == base.initial_state()
    assert game.public_key(state) == base.public_key(state)
    assert game.rules_version != base.rules_version
    assert game.game_id == base.game_id


def test_global_relabeling_preserves_infosets_legal_actions_and_own_recall():
    game = SuitCanonicalHoldem(HoldemGame(stacks=(8, 8, 8)))
    rng = Random(91)
    observed_actions = {}
    recalled = {}
    for _ in range(30):
        state = game.initial_state()
        own_history = defaultdict(tuple)
        while (actor := game.actor(state)) is not None:
            if actor == -1:
                action = game.sample_chance(state, rng)
            else:
                key = (actor, game.information_key(state, actor))
                actions = game.legal_actions(state)
                assert observed_actions.setdefault(key, actions) == actions
                assert recalled.setdefault(key, own_history[actor]) == own_history[actor]
                mapping = dict(zip(SUITS, rng.sample(list(SUITS), 4), strict=True))
                remap = lambda c, mapping=mapping: c[0] + mapping[c[1]]  # noqa: E731
                relabeled = replace(
                    state,
                    board=tuple(map(remap, state.board)),
                    hole_cards=tuple(tuple(map(remap, hand)) for hand in state.hole_cards),
                )
                assert game.information_key(relabeled, actor) == key[1]
                assert game.legal_actions(relabeled) == actions
                action = rng.choice(actions)
                own_history[actor] += ((key[1], action),)
            state = game.transition(state, action)


def test_abstract_artifact_cannot_load_into_exact_game(tmp_path):
    base = HoldemGame(stacks=(4, 4))
    game = SuitCanonicalHoldem(base)
    trainer = Trainer(game, algorithm="mccfr", seed=0)
    assert trainer.train(10).completed_iterations == 10
    blueprint = Blueprint.from_trainer(trainer)
    path = tmp_path / "abstract.json"
    blueprint.save(path)
    loaded = Blueprint.load(path, game=game)
    assert loaded.policy.entries == blueprint.policy.entries
    with pytest.raises(ValueError, match="incompatible"):
        Blueprint.load(path, game=base)


def test_preflop_coverage_transfers_only_to_suit_isomorphic_hands():
    base = HoldemGame(stacks=(4, 4))
    game = SuitCanonicalHoldem(base)
    first = dealt(base, ("As", "Qc", "Ks", "Jd"))
    mapped = dealt(base, ("Ah", "Qd", "Kh", "Jc"))
    different = dealt(base, ("Ah", "Qd", "Kc", "Jd"))
    actor = game.actor(first)
    assert base.information_key(first, actor) != base.information_key(mapped, actor)
    assert game.information_key(first, actor) == game.information_key(mapped, actor)
    assert game.information_key(first, actor) != game.information_key(different, actor)


def test_invalid_or_repeated_cards_are_rejected():
    with pytest.raises(ValueError):
        preflop_class(("As", "As"))
    with pytest.raises(ValueError):
        canonical_card_trace(("As", "Kh"), ("As", "2c", "3d"))
