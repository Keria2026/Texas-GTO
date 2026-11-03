"""Opening escrow is explicit query context, never inferred live evidence."""

import json
from dataclasses import fields
from random import Random

import pytest

from texas_game_core.mushroom_game import MushroomSessionGame
from texas_game_core.public_holdem import HoldemRules, PublicHoldemGame, PublicHoldemState
from texas_strategy.blueprint import _trainer_metadata
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import TraversalBudget
from texas_strategy.mushroom_abstraction import MushroomContextGame
from texas_strategy.mushroom_belief import MushroomFirstHandRoot
from texas_strategy.mushroom_public import build_public_query, lookup_public_policy
from texas_strategy.persistent_policy import policy_snapshot
from texas_strategy.policy import TabularPolicy
from texas_strategy.training_store import open_trainer


def model(*, outstanding=None, waiting=(), stacks=(800,) * 8, button=0):
    return MushroomContextGame(
        MushroomSessionGame(
            stacks,
            button=button,
            horizon=2,
            outstanding=outstanding,
            waiting_for_top_up=waiting,
        )
    )


def decision(game, state=None, rng=None):
    state = game.initial_state() if state is None else state
    rng = Random(31) if rng is None else rng
    while game.actor(state) == -1:
        action, _ = game.sample_chance_outcome(state, rng)
        state = game.transition(state, action)
    return state


def facts(game, state):
    table = state.underlying.table
    hand, engine = table.hand.state, table.hand.game
    public_game = PublicHoldemGame(
        HoldemRules(
            engine.initial_stacks,
            small_blind=engine.small_blind,
            big_blind=engine.big_blind,
            straddle=engine.straddle,
            button=engine.button,
        )
    )
    public = PublicHoldemState(
        **{
            f.name: getattr(hand, f.name)
            for f in fields(PublicHoldemState)
            if f.name != "awaiting_settlement"
        }
    )
    ledger = dict(table.ledger.snapshot().outstanding_contributions)
    return dict(
        model=game,
        hand_game=public_game,
        public_state=public,
        table_button=table.button,
        policy_anchor=game.base_game.button,
        hand_offset=table.hand_number - 1,
        participants=table.participants,
        stacks=table.stacks,
        outstanding=tuple(ledger.get(str(p), 0) for p in range(game.num_players)),
        pool=table.pool,
        waiting_for_top_up=table.waiting_for_top_up,
        hero_seat=table.participants[hand.to_act],
        hero_cards=hand.hole_cards[hand.to_act],
    )


def test_explicit_empty_opening_preserves_legacy_context_and_seeded_training(tmp_path):
    first = model(stacks=(64,) * 4)
    second = model(stacks=(64,) * 4, outstanding=(0,) * 4, waiting=())
    assert first.base_game.rules_version == second.base_game.rules_version
    assert first.rules_version == second.rules_version
    assert first.checkpoint_context == second.checkpoint_context
    paths = []
    for i, game in enumerate((first, second)):
        trainer = Trainer(game, algorithm="mccfr", seed=19)
        assert trainer.train(2, max_nodes=100_000).completed_iterations == 2
        path = tmp_path / f"model-{i}.json"
        trainer.save_checkpoint(path)
        paths.append(path)
    assert paths[0].read_bytes() == paths[1].read_bytes()


@pytest.mark.parametrize("waiting", [(), (6, 7)])
def test_opening_context_binds_training_restore_but_not_readonly_policy_family(tmp_path, waiting):
    original = model()
    seeded = model(outstanding=(20, 40, 20, 20, 20, 20, 20, 20), waiting=waiting)
    assert original.rules_version == seeded.rules_version
    assert original.checkpoint_context != seeded.checkpoint_context
    assert original.base_game.rules_version != seeded.base_game.rules_version
    context = json.loads(seeded.checkpoint_context)
    assert context["openingOutstanding"] == list(seeded.base_game.initial_outstanding)
    assert context.get("openingWaiting", []) == list(waiting)
    path, checkpoint = tmp_path / "core.sqlite3", tmp_path / "core.json"
    writer = open_trainer(original, path)
    writer._store.close()
    Trainer(original, algorithm="pluribus").save_checkpoint(checkpoint)
    for load in (
        lambda: open_trainer(seeded, path),
        lambda: Trainer.load_checkpoint(seeded, checkpoint),
    ):
        with pytest.raises(ValueError, match="context"):
            load()
    query = build_public_query(**facts(seeded, decision(seeded)))
    with policy_snapshot(seeded, path) as policy:
        assert lookup_public_policy(policy, query) is None
        assert policy.fallback_count == 0
    # Context reuse must not manufacture a trained average for this different opening.
    assert (
        query.information_key
        != build_public_query(**facts(original, decision(original))).information_key
    )


@pytest.mark.parametrize("count,waiting,button", [(4, (), 2), (8, (), 5), (8, (6, 7), 7)])
def test_two_hand_public_queries_keep_opening_escrow_and_waiting_balances(count, waiting, button):
    game = model(stacks=(800,) * count, outstanding=(20,) * count, waiting=waiting, button=button)
    state, rng, rounds = game.initial_state(), Random(17), set()
    while (actor := game.actor(state)) is not None:
        if actor == -1:
            action, _ = game.sample_chance_outcome(state, rng)
        else:
            query = build_public_query(**facts(game, state))
            assert (query.player, query.information_key, query.action_map) == (
                actor,
                game.information_key(state, actor),
                game.action_map(state),
            )
            rounds.add(game.betting_round(state))
            assert all(state.underlying.table.stacks[p] == 800 for p in waiting)
            action = "check" if "check" in query.actions else "call"
        state = game.transition(state, action)
    assert rounds == set(range(8))
    assert sum(game.terminal_payoffs(state)) == 0
    assert sum(state.underlying.table.stacks) == 820 * count


def test_first_hand_binding_rejects_changed_ownership_even_when_pool_total_matches():
    game = model(outstanding=(20,) * 8)
    observed = facts(game, decision(game))
    wrong = list(observed["outstanding"])
    wrong[0] += 1
    wrong[1] -= 1
    with pytest.raises(ValueError, match="escrow"):
        build_public_query(**{**observed, "outstanding": tuple(wrong)})
    with pytest.raises(ValueError):
        build_public_query(**{**observed, "model": model()})


def test_generated_first_hand_root_preserves_existing_escrow_without_using_hero_cards():
    game = model(outstanding=(20, 60, 40, 20, 80, 20, 40, 20), waiting=(6, 7), button=7)
    observed = facts(game, decision(game))
    policy = TabularPolicy(missing="error")
    policy.metadata = _trainer_metadata(Trainer(game, algorithm="pluribus"))
    root = MushroomFirstHandRoot(game, observed["public_state"], policy, budget=TraversalBudget())
    target = root.target_information_key(observed["hero_seat"], observed["hero_cards"])
    actual = decision(game)
    assert target == game.search_information_key(actual, game.actor(actual))
    private_samples = set()
    for seed in range(8):
        sample, q, w = root.root.sample(Random(seed))
        public = facts(game, sample)
        assert q == w > 0
        assert game.public_key(sample) == root.public_key
        assert public["pool"] == observed["pool"]
        assert public["outstanding"] == observed["outstanding"]
        assert public["stacks"] == observed["stacks"]
        assert public["waiting_for_top_up"] == (6, 7)
        private_samples.add(public["hero_cards"])
    assert len(private_samples) > 1
    assert policy.fallback_count == 0


def test_eight_player_pluribus_resumes_the_exact_seeded_window(tmp_path):
    game = model(outstanding=(20, 60, 40, 20, 80, 20, 40, 20))
    trainer = Trainer(game, algorithm="pluribus", seed=42)
    assert trainer.train(1, max_nodes=250_000).completed_iterations == 1
    checkpoint = tmp_path / "opening.json"
    trainer.save_checkpoint(checkpoint)
    restored = Trainer.load_checkpoint(game, checkpoint)
    for instance in (trainer, restored):
        assert instance.train(1, max_nodes=250_000).completed_iterations == 1
    first, second = tmp_path / "continued.json", tmp_path / "restored.json"
    trainer.save_checkpoint(first)
    restored.save_checkpoint(second)
    assert first.read_bytes() == second.read_bytes()
