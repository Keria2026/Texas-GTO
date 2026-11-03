from random import Random

import pytest
from test_mushroom_belief import SyntheticPolicy, observed_prefix, prefix_policy, strip_private

from texas_strategy.behavior_policy import compare_actions
from texas_strategy.blueprint import _trainer_metadata
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import TraversalBudget
from texas_strategy.mushroom_belief import MushroomStreetRoot
from texas_strategy.policy import PolicyEntry, TabularPolicy
from texas_strategy.search import (
    DepthLimitedGame,
    SampledRoot,
    SearchState,
    solve_generated_subgame,
)


class ResponseGame:
    """Eight players, with a choice of betting into a caller or a folder."""

    num_players = 8
    game_id = rules_version = "response-test"

    def initial_state(self):
        return ()

    def actor(self, state):
        return len(state) if len(state) < 2 else None

    def legal_actions(self, state):
        return ("check", "bet") if not state else ("fold", "call")

    def information_key(self, state, player):
        return str(state)

    public_key = information_key

    def transition(self, state, action):
        return (*state, action)

    def terminal_payoffs(self, state):
        value = 0 if state[0] == "check" else 1 if state[1] == "fold" else -2
        return (value, -value, *(0,) * 6)


@pytest.mark.parametrize("algorithm", ["cfr", "mccfr"])
@pytest.mark.parametrize("call", [0.0, 1.0])
def test_fixed_opponent_changes_eight_player_optimal_action_and_is_not_core(algorithm, call):
    def fixed(state, key, actions):
        return PolicyEntry(actions, (1 - call, call)) if key[0] == 1 else None

    trainer = Trainer(ResponseGame(), algorithm=algorithm, seed=2, fixed_policy=fixed)
    trainer.train(100)
    current = trainer.current_policy()
    assert current.lookup(0, "()").probabilities == ((1.0, 0.0) if call else (0.0, 1.0))
    assert current.lookup(1, "('check',)" if call else "('bet',)").probabilities == (1 - call, call)
    before, rng = dict(current.entries), trainer._rng.getstate()
    assert trainer.train(1, max_nodes=2).completed_iterations == 0
    assert trainer._rng.getstate() == rng
    assert trainer.current_policy().entries == before
    with pytest.raises(ValueError, match="cannot be persisted"):
        _trainer_metadata(trainer)


def test_descendant_target_is_queried_at_its_actual_depth():
    class Game(ResponseGame):
        def public_key(self, state):
            return str(state)

        def active_players(self, state):
            return tuple(range(8))

    class Policy:
        def probabilities(self, game, state):
            return (0.5, 0.5)

    game = Game()
    result = solve_generated_subgame(
        game,
        SampledRoot("()", 0, lambda rng: ((), 1.0, 1.0)),
        Policy(),
        target_player=1,
        target_information_key="('bet',)",
        target_depth=1,
        depth_limit=2,
        iterations=20,
    )
    assert result.policy.lookup(1, DepthLimitedGame.root_information_key("('bet',)", 1))
    assert result.actions == ("fold", "call")
    with pytest.raises(ValueError, match="cutoff"):
        solve_generated_subgame(
            game,
            SampledRoot("()", 0, lambda rng: ((), 1.0, 1.0)),
            Policy(),
            target_player=1,
            target_information_key="('bet',)",
            target_depth=2,
        )


@pytest.mark.parametrize("button", [0, 3, 7])
def test_street_root_removes_only_current_street_actions_and_keeps_full_range(button):
    model, state, history = observed_prefix(button=button, flop=True, actions=2)
    policy = prefix_policy(model, state, history)
    root = MushroomStreetRoot(model, strip_private(state), policy, budget=TraversalBudget(100000))
    assert root.target_depth == 2
    assert root._state.street == root.target_state.street == 1
    assert len(root._state.history) == len(root.target_state.history) - 2
    rng = Random(2)
    samples = [root._sample(rng)[0] for _ in range(5)]
    assert len({s.underlying.table.hand.state.hole_cards for s in samples}) == 5
    for sample in samples:
        assert model.public_key(sample) == root.public_key
        for event in root.target_state.history[root.root_history :]:
            sample = model.transition(sample, event.action)
        assert strip_private(sample) == root.target_state


@pytest.mark.parametrize("call,accept", [(False, True), (True, False)])
def test_independent_rollouts_accept_profitable_mixture_and_reject_losing_one(call, accept):
    class Policy:
        def probabilities(self, game, state):
            return (0.0, 1.0) if call else (1.0, 0.0)

    result = compare_actions(
        ResponseGame(),
        lambda rng: ((), 1.0),
        Policy(),
        0,
        PolicyEntry(("check", "bet"), (1.0, 0.0)),
        PolicyEntry(("check", "bet"), (0.75, 0.25)),
        budget=TraversalBudget(),
        seed=24,
    )
    assert result["accepted"] is accept
    assert result["samples"] == result["effective_samples"] == 32
    assert result["lower_bound"] == pytest.approx(
        result["delta_points"] - 1.96 * result["standard_error"]
    )


def test_cancelled_independent_comparison_does_not_return_partial_evidence():
    from texas_strategy.evaluation import CancellationError

    with pytest.raises(CancellationError):
        compare_actions(
            ResponseGame(),
            lambda rng: ((), 1.0),
            None,
            0,
            PolicyEntry(("check", "bet"), (1.0, 0.0)),
            PolicyEntry(("check", "bet"), (0.75, 0.25)),
            budget=TraversalBudget(should_cancel=lambda: True),
            seed=24,
        )


def test_real_eight_seat_engine_search_queries_a_descendant_not_the_root_actor():
    model, actual, history = observed_prefix(flop=True, actions=1)

    class ExplicitResearchPolicy(SyntheticPolicy):
        def contains(self, game, state):
            return True

        def probabilities(self, game, state):
            return (1 / len(game.legal_actions(state)),) * len(game.legal_actions(state))

    policy = ExplicitResearchPolicy(model, prefix_policy(model, actual, history).entries)
    root = MushroomStreetRoot(model, strip_private(actual), policy, budget=TraversalBudget(300000))
    hand = actual.underlying.table.hand.state
    result = root.solve(
        actual.underlying.table.participants[hand.to_act],
        hand.hole_cards[hand.to_act],
        iterations=12,
        seed=17,
        max_nodes=300000,
        max_rollout_nodes=300000,
    )
    assert root.target_depth == 1 and result.player != root.actor
    assert result.information_key == model.search_information_key(actual, model.actor(actual))
    assert result.diagnostics.completed_iterations == 12
    assert result.actions == model.legal_actions(actual)
    assert sum(result.probabilities) == pytest.approx(1)


def test_only_actual_hero_prior_policy_is_frozen_and_published_baseline_is_retained(monkeypatch):
    import texas_strategy.mushroom_belief as belief

    model, first, _ = observed_prefix()
    hero = first.underlying.table.hand.state.to_act
    player = model.actor(first)
    cards = first.underlying.table.hand.state.hole_cards[hero]
    target = model.transition(first, "call")
    target = model.transition(target, "raise_min")
    while model.actor(target) != player:
        target = model.transition(target, "call")
    actions = tuple(model.legal_actions(first))
    entry = PolicyEntry(actions, tuple(float(a == "call") for a in actions))
    published = TabularPolicy(
        {(player, model.information_key(first, player)): entry}, missing="error"
    )
    record = {
        "street": 0,
        "root_history": 0,
        "current": TabularPolicy(),
        "average": TabularPolicy(),
        "published": published,
    }
    root = MushroomStreetRoot(
        model,
        strip_private(target),
        SyntheticPolicy(model, {}),
        budget=TraversalBudget(),
        previous=(record,),
    )
    monkeypatch.setattr(belief, "solve_generated_subgame", lambda *a, **kw: kw["fixed_policy"])
    fixed = root.solve(hero, cards)
    assert fixed(SearchState(first), (player, "query"), actions) == entry
    other, _, _ = root._sample(Random(123))
    assert other.underlying.table.hand.state.hole_cards[hero] != cards
    assert fixed(SearchState(other), (player, "different-private-hand"), actions) is None


@pytest.mark.parametrize("cached_pair", [False, True])
def test_previous_average_keeps_private_overrides_without_repeating_blueprint_reads(cached_pair):
    model, actual, history = observed_prefix(flop=True)
    entries = prefix_policy(model, actual, history).entries
    first, observed = history[0]
    player = model.actor(first)
    hand = first.underlying.table.hand.state
    physical = first.underlying.table.participants[hand.to_act]
    cards = tuple(sorted(hand.hole_cards[hand.to_act]))
    actions = tuple(model.legal_actions(first))
    average = {}
    if cached_pair:
        average[
            player,
            DepthLimitedGame.root_information_key(model.search_information_key(first, player)),
        ] = PolicyEntry(
            actions, tuple(0 if a == observed else 1 / (len(actions) - 1) for a in actions)
        )
    record = {
        "street": 0,
        "root_history": 0,
        "current": TabularPolicy(missing="error"),
        "average": TabularPolicy(average, missing="error"),
    }
    policy = SyntheticPolicy(model, entries)
    root = MushroomStreetRoot(
        model, strip_private(actual), policy, budget=TraversalBudget(100000), previous=(record,)
    )
    assert policy.lookups == len(entries)
    reference = MushroomStreetRoot(
        model,
        strip_private(actual),
        SyntheticPolicy(model, entries),
        budget=TraversalBudget(100000),
    )
    if cached_pair:
        reference._likelihoods[physical][cards] = float("-inf")
    assert root._likelihoods == reference._likelihoods


def test_first_hand_hero_constraints_never_freeze_the_next_hand(monkeypatch):
    import texas_strategy.mushroom_belief as belief
    from texas_game_core.cards import DECK

    model, first, _ = observed_prefix()
    first_hero = model.transition(first, "call")
    hero = first_hero.underlying.table.hand.state.to_act
    player = model.actor(first_hero)
    cards = tuple(sorted(first_hero.underlying.table.hand.state.hole_cards[hero]))
    target = model.transition(first_hero, "call")
    target = model.transition(target, "raise_min")
    while model.actor(target) != player:
        target = model.transition(target, "call")
    root = MushroomStreetRoot(
        model, strip_private(target), SyntheticPolicy(model, {}), budget=TraversalBudget()
    )
    monkeypatch.setattr(belief, "solve_generated_subgame", lambda *a, **kw: kw["fixed_policy"])
    fixed = root.solve(hero, cards)
    future = first
    depth = 0
    while future.underlying.table.hand_number == 1:
        future = model.transition(future, "fold")
        depth += 1
    while model.actor(future) == -1:
        hand = future.underlying.table.hand.state
        recipient = hand.pending_deal[0]
        used = {card for pair in hand.hole_cards for card in pair}
        card = (
            cards[len(hand.hole_cards[recipient])]
            if recipient == hero
            else next(card for card in DECK if card not in used and card not in cards)
        )
        future = model.transition(future, f"deal:{card}")
    assert future.underlying.table.hand_number == 2 and model.actor(future) == player
    assert tuple(sorted(future.underlying.table.hand.state.hole_cards[hero])) == cards
    assert (
        fixed(
            SearchState(future, depth=depth),
            (player, "next-hand"),
            tuple(model.legal_actions(future)),
        )
        is None
    )
