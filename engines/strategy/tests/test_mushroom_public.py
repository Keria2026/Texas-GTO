"""Observed public facts must select exactly the existing trained information set."""

from dataclasses import fields, replace
from random import Random

import pytest

from texas_game_core.cards import DECK
from texas_game_core.mushroom_game import MushroomSessionGame
from texas_game_core.public_holdem import HoldemRules, PublicHoldemGame, PublicHoldemState
from texas_strategy.cfr import Trainer, _TableEntry
from texas_strategy.mushroom_abstraction import MushroomContextGame
from texas_strategy.mushroom_public import build_public_query, lookup_public_policy
from texas_strategy.persistent_policy import policy_snapshot
from texas_strategy.training_store import open_trainer


def public_facts(model, state):
    table, hand = state.underlying.table, state.underlying.table.hand
    engine, inner = hand.game, hand.state
    public_game = PublicHoldemGame(
        HoldemRules(
            engine.initial_stacks,
            small_blind=engine.small_blind,
            big_blind=engine.big_blind,
            straddle=engine.straddle,
            ante=engine.ante,
            button=engine.button,
        )
    )
    values = {
        field.name: getattr(inner, field.name)
        for field in fields(PublicHoldemState)
        if field.name not in ("rules_version", "awaiting_settlement")
    }
    values.update(rules_version=public_game.rules_version, awaiting_settlement=inner.terminal)
    public = PublicHoldemState(**values)
    hero = table.participants[inner.to_act]
    fees = dict(table.ledger.snapshot().outstanding_contributions)
    return {
        "model": model,
        "hand_game": public_game,
        "public_state": public,
        "table_button": table.button,
        "policy_anchor": model.base_game.button,
        "hand_offset": table.hand_number - 1,
        "participants": table.participants,
        "stacks": table.stacks,
        "outstanding": tuple(fees.get(str(seat), 0) for seat in range(model.num_players)),
        "pool": table.pool,
        "waiting_for_top_up": table.waiting_for_top_up,
        "hero_seat": hero,
        "hero_cards": inner.hole_cards[inner.to_act],
    }


def at_decision(model, state=None, seed=42):
    state = model.initial_state() if state is None else state
    rng = Random(seed)
    while model.actor(state) == -1:
        action, _ = model.sample_chance_outcome(state, rng)
        state = model.transition(state, action)
    return state


@pytest.mark.parametrize("players,button", [(4, 0), (4, 2), (8, 5)])
@pytest.mark.parametrize("ante", [0, 4])
def test_public_queries_match_complete_two_hand_trajectories(players, button, ante):
    model = MushroomContextGame(
        MushroomSessionGame((800,) * players, button=button, horizon=2, ante=ante)
    )
    state, rng = model.initial_state(), Random(31)
    rounds = set()
    while (actor := model.actor(state)) is not None:
        if actor == -1:
            action, _ = model.sample_chance_outcome(state, rng)
        else:
            query = build_public_query(**public_facts(model, state))
            assert query.player == actor
            assert query.information_key == model.information_key(state, actor)
            assert query.action_map == model.action_map(state)
            rounds.add(model.betting_round(state))
            action = "check" if "check" in query.actions else "call"
        state = model.transition(state, action)
    assert rounds == set(range(8))


def test_legacy_initial_and_first_decision_hashes_are_frozen():
    model = MushroomContextGame(MushroomSessionGame((800,) * 4))
    assert model.information_key(model.initial_state(), 0) == (
        "f7c86a60b837199e623a1069cc51fa53066fcdff6cb00fa80fe60cbf663c63aa"
    )
    state = at_decision(model)
    assert build_public_query(**public_facts(model, state)).information_key == (
        "fd38598d05735bd7443e1fedb368f202bb04bc77b5825fd921dd9ae9faf948dc"
    )


def test_opponent_holes_are_absent_and_cannot_affect_the_query(monkeypatch):
    model = MushroomContextGame(MushroomSessionGame())
    state = at_decision(model)
    facts = public_facts(model, state)
    table = state.underlying.table.fork()
    holes = list(table.hand.state.hole_cards)
    other = (table.hand.state.to_act + 1) % len(holes)
    used = {card for hole in holes for card in hole}
    holes[other] = tuple(card for card in DECK if card not in used)[:2]
    table.hand = replace(table.hand, state=replace(table.hand.state, hole_cards=tuple(holes)))
    altered = replace(state, underlying=replace(state.underlying, table=table))
    altered_facts = public_facts(model, altered)
    assert not hasattr(facts["public_state"], "hole_cards")
    assert facts["public_state"] == altered_facts["public_state"]

    def forbidden(*args, **kwargs):
        pytest.fail("A public query must not sample any private cards")

    monkeypatch.setattr(model, "sample_chance_outcome", forbidden)
    monkeypatch.setattr(model.base_game, "sample_chance_outcome", forbidden)
    assert build_public_query(**facts) == build_public_query(**altered_facts)


def test_off_tree_history_is_preserved_and_forged_amounts_or_markers_are_rejected():
    model = MushroomContextGame(MushroomSessionGame())
    state = model.transition(at_decision(model), "raise_to:17")
    facts = public_facts(model, state)
    query = build_public_query(**facts)
    assert state.actions[-1][2] == "raise_to:17"
    assert query.information_key == model.information_key(state, model.actor(state))
    event = facts["public_state"].history[-1]
    assert not event.in_action_abstraction
    for changed in (
        replace(event, in_action_abstraction=True),
        replace(event, amount_delta=event.amount_delta + 1),
        replace(event, amount_to=18),
    ):
        forged = replace(facts["public_state"], history=(changed,))
        with pytest.raises(ValueError):
            build_public_query(**{**facts, "public_state": forged})


def test_window_offset_and_policy_anchor_are_explicit_coordinates():
    model = MushroomContextGame(MushroomSessionGame())
    facts = public_facts(model, at_decision(model))
    first = build_public_query(**facts)
    later = build_public_query(**{**facts, "hand_offset": 1})
    assert later.information_key != first.information_key
    # The model permits policy reuse, but seat indices are anchored to its initial button.
    rotated_anchor = MushroomContextGame(MushroomSessionGame(button=3))
    anchored = build_public_query(
        **{**facts, "model": rotated_anchor, "policy_anchor": 3, "hand_offset": 1}
    )
    assert anchored.information_key == later.information_key
    assert anchored.player == (facts["hero_seat"] - 3) % 8
    assert anchored.player != later.player
    for changes in (
        {"policy_anchor": 3},
        {"hand_offset": None},
        {"hand_offset": 2},
        {"table_button": 1},
    ):
        with pytest.raises(ValueError):
            build_public_query(**{**facts, **changes})


@pytest.mark.parametrize(
    "field",
    ["pool", "hand_offset", "policy_anchor", "table_button", "outstanding", "waiting_for_top_up"],
)
def test_missing_facts_have_no_implicit_defaults(field):
    model = MushroomContextGame(MushroomSessionGame())
    facts = public_facts(model, at_decision(model))
    del facts[field]
    with pytest.raises(TypeError):
        build_public_query(**facts)


def test_unknown_cards_wrong_actor_and_inconsistent_ledger_or_stacks_are_rejected():
    model = MushroomContextGame(MushroomSessionGame())
    facts = public_facts(model, at_decision(model))
    for changes in (
        {"hero_cards": (None, "As")},
        {"hero_cards": ("As", "As")},
        {"hero_seat": (facts["hero_seat"] + 1) % 8},
        {"outstanding": (0,) * 8},
        {"outstanding": (20,) * 7},
        {"pool": None},
        {"stacks": (800,) * 8},
        {"waiting_for_top_up": (0,)},
        {"participants": tuple(reversed(facts["participants"]))},
    ):
        with pytest.raises((TypeError, ValueError)):
            build_public_query(**{**facts, **changes})


def test_funded_first_window_seat_cannot_be_falsely_marked_waiting():
    actual = MushroomContextGame(MushroomSessionGame((800,) * 7 + (20,)))
    facts = public_facts(actual, at_decision(actual))
    # Global funding balances, but an 800-point seat cannot be excluded in this first hand.
    model = MushroomContextGame(MushroomSessionGame())
    with pytest.raises(ValueError, match="First-hand"):
        build_public_query(**{**facts, "model": model, "stacks": (*facts["stacks"][:7], 800)})


@pytest.mark.parametrize("stacks", [(20, 800, 800, 800, 800, 800, 800, 800), (800,) * 6 + (10, 20)])
def test_waiting_seats_and_skipped_blinds_keep_exact_policy_coordinates(stacks):
    model = MushroomContextGame(MushroomSessionGame(stacks))
    state = at_decision(model)
    facts = public_facts(model, state)
    query = build_public_query(**facts)
    assert facts["waiting_for_top_up"]
    assert query.player == model.actor(state)
    assert query.information_key == model.information_key(state, query.player)
    assert query.action_map == model.action_map(state)
    if len(facts["waiting_for_top_up"]) == 2:
        with pytest.raises(ValueError, match="First-hand"):
            build_public_query(**{**facts, "stacks": (*facts["stacks"][:6], 20, 10)})


def test_public_key_reads_the_same_persisted_average_and_never_invents_missing_coverage(tmp_path):
    model = MushroomContextGame(MushroomSessionGame())
    state = at_decision(model)
    query = build_public_query(**public_facts(model, state))
    trainer = Trainer(model, algorithm="pluribus", seed=42)
    trainer.iterations = 1
    sums = list(range(1, len(query.actions) + 1))
    trainer._table[(query.player, query.information_key)] = _TableEntry(
        query.actions, [0.0] * len(sums), sums
    )
    trainer._betting_rounds[(query.player, query.information_key)] = 0
    legacy, database = tmp_path / "legacy.json", tmp_path / "policy.sqlite3"
    trainer.save_checkpoint(legacy)
    durable = open_trainer(model, database, checkpoint=legacy)
    durable._store.close()
    with policy_snapshot(model, database) as policy:
        entry = lookup_public_policy(policy, query)
        assert entry.probabilities == pytest.approx([value / sum(sums) for value in sums])
        assert entry.probabilities == policy.probabilities(model, state)
        assert query.action_map["all_in"] == "raise_to:780"
        assert lookup_public_policy(policy, replace(query, information_key="uncovered")) is None
        assert policy.fallback_count == 0
        with pytest.raises(ValueError, match="model"):
            lookup_public_policy(policy, replace(query, rules_version="other"))
        with pytest.raises(ValueError, match="legal actions"):
            lookup_public_policy(policy, replace(query, actions=tuple(reversed(query.actions))))
