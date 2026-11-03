"""Streaming public roots retain full support and use the same continuation game."""

from dataclasses import dataclass
from random import Random

import pytest

from texas_game_core.kuhn import KuhnPoker
from texas_game_core.leduc import LeducPoker
from texas_strategy import search
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import CancellationError, EvaluationLimitError
from texas_strategy.policy import TabularPolicy
from texas_strategy.search import (
    DepthLimitedGame,
    SampledRoot,
    SearchCoverageError,
    build_public_root,
    solve_generated_subgame,
    solve_subgame,
)


class UniformPolicy:
    def probabilities(self, game, state):
        actions = game.legal_actions(state)
        return (1 / len(actions),) * len(actions)


def finite_sampler(root, *, scale=1.0):
    def sample(rng):
        threshold, cumulative = rng.random(), 0.0
        for state, probability in zip(root.states, root.probabilities, strict=True):
            cumulative += probability
            if threshold < cumulative:
                return state, probability, probability * scale
        return root.states[-1], root.probabilities[-1], root.probabilities[-1] * scale

    return sample


@pytest.mark.parametrize("game", [KuhnPoker(), LeducPoker()])
@pytest.mark.parametrize("scale", [1.0, 0.125])
def test_generated_and_finite_public_roots_have_identical_seeded_policy(game, scale):
    target = game.replay((2, 0))
    blueprint = UniformPolicy()
    root = build_public_root(game, game.public_key(target), blueprint)
    options = dict(depth_limit=1, iterations=40, seed=13)
    finite = solve_subgame(
        game,
        target,
        blueprint,
        root_distribution=root,
        algorithm="mccfr",
        sampled_rollouts=True,
        **options,
    )
    generated = solve_generated_subgame(
        game,
        SampledRoot(root.public_key, 0, finite_sampler(root, scale=scale)),
        blueprint,
        target_player=0,
        target_information_key=game.information_key(target, 0),
        **options,
    )
    assert generated.policy.entries == finite.policy.entries
    assert generated.actions == finite.actions
    assert generated.probabilities == finite.probabilities
    assert generated.subgame.rollout_samples == finite.subgame.rollout_samples
    report = generated.to_dict()
    assert report["root_draws"] == report["root_positive_draws"] == 80
    assert report["root_effective_sample_size"] == pytest.approx(80)
    assert report["root_worlds"] is report["root_reach_probability"] is None
    assert report["root_source"] == "generated_public_belief"
    assert report["root_coverage"] == "sampled_target_only_full_range_not_certified"
    assert "root_draws" not in finite.to_dict()
    assert not hasattr(finite.subgame, "sample_training_root")


@dataclass(frozen=True, slots=True)
class LargeState:
    world: int
    action: str | None = None


class LargeGenerativeGame:
    """4,096 latent assignments; neither the game nor the callback stores a world list."""

    num_players = 2
    game_id = "generative-test"
    rules_version = "generative-test-v1"

    def initial_state(self):
        raise AssertionError("generated search must not enumerate or build the original root")

    def chance_outcomes(self, state):
        raise AssertionError("there is no enumerated private-world interface")

    def actor(self, state):
        return 0 if state.action is None else None

    def public_key(self, state):
        return "public-root" if state.action is None else state.action

    def legal_actions(self, state):
        return ("check", "bet") if state.action is None else ()

    def information_key(self, state, player):
        return f"abstract:{state.world // 64}"

    def search_information_key(self, state, player):
        return f"private:{state.world // 64}"

    def transition(self, state, action):
        assert action in self.legal_actions(state)
        return LargeState(state.world, action)

    def active_players(self, state):
        return (0, 1)

    def terminal_payoffs(self, state):
        assert state.action is not None
        value = ((state.world % 64) - 32) * (1 if state.action == "check" else 2)
        return value, -value


def test_more_than_512_possible_worlds_stream_without_conditioning_on_target_hand():
    game = LargeGenerativeGame()
    seen = set()

    def sample(rng):
        world = rng.randrange(4096)
        seen.add(world)
        return LargeState(world), 1 / 4096, 1 / 8192

    root = SampledRoot("public-root", 0, sample)
    result = solve_generated_subgame(
        game,
        root,
        UniformPolicy(),
        target_player=0,
        target_information_key="private:2",
        iterations=600,
        seed=17,
    )
    assert len(seen) > 512
    assert len({world // 64 for world in seen}) == 64
    assert result.actions == ("check", "bet")
    assert result.information_key == "private:2"
    assert result.subgame.root_distribution is root
    assert not hasattr(root, "states") and not hasattr(result.subgame, "_root_labels")
    assert result.to_dict()["root_draws"] == 1200
    assert len(result.policy.entries) == 64


@pytest.mark.parametrize("zero", [False, True])
def test_unseen_target_and_all_zero_draws_fail_without_invented_coverage(zero):
    game = LargeGenerativeGame()
    draws = 0

    def sample(rng):
        nonlocal draws
        draws += 1
        return LargeState(rng.randrange(4096)), 1 / 4096, 0 if zero else 1 / 4096

    with pytest.raises(SearchCoverageError, match="did not train the requested"):
        solve_generated_subgame(
            game,
            SampledRoot("public-root", 0, sample),
            UniformPolicy(),
            target_player=0,
            target_information_key="private:999" if not zero else "private:0",
            iterations=12,
        )
    assert draws == 24  # Zero weights are consumed once, never silently resampled.


def test_zero_worlds_never_enter_average_coverage_and_ess_is_scale_safe():
    game = LargeGenerativeGame()
    draws = iter(
        ((LargeState(0), 0.25, 0.0), (LargeState(64), 0.25, 2e307), (LargeState(128), 0.5, 2e307))
    )
    subgame = DepthLimitedGame(
        game,
        SampledRoot("public-root", 0, lambda rng: next(draws)),
        UniformPolicy(),
        sampled_rollouts=True,
    )
    for _ in range(3):
        subgame.sample_training_root(Random(0))
    assert subgame.root_draws == 3 and subgame.root_positive_draws == 2
    assert subgame.root_effective_sample_size == pytest.approx(1.8)

    root = SampledRoot("public-root", 0, lambda rng: (None, 1.0, 0.0))
    trainer = Trainer(
        DepthLimitedGame(game, root, UniformPolicy(), sampled_rollouts=True), algorithm="mccfr"
    )
    assert trainer.train(2).completed_iterations == 2
    assert not trainer.average_policy(missing="error").entries


@pytest.mark.parametrize(
    "q,w",
    [
        (0, 1),
        (1.1, 1),
        (True, 1),
        (float("nan"), 1),
        (0.5, -1),
        (0.5, float("inf")),
        (1e-308, 1e308),
    ],
)
def test_invalid_proposal_weights_fail_before_a_draw_is_counted(q, w):
    subgame = DepthLimitedGame(
        LargeGenerativeGame(),
        SampledRoot("public-root", 0, lambda rng: (LargeState(0), q, w)),
        UniformPolicy(),
        sampled_rollouts=True,
    )
    with pytest.raises(ValueError, match="finite q, w"):
        subgame.sample_training_root(Random(0))
    assert subgame.root_draws == 0


@pytest.mark.parametrize("key,actor", [("different-root", 0), ("public-root", 1)])
def test_every_generated_world_must_match_public_history_and_actor(key, actor):
    subgame = DepthLimitedGame(
        LargeGenerativeGame(),
        SampledRoot(key, actor, lambda rng: (LargeState(0), 1.0, 1.0)),
        UniformPolicy(),
        sampled_rollouts=True,
    )
    with pytest.raises(SearchCoverageError, match="fixed public history and actor"):
        subgame.sample_training_root(Random(0))
    assert subgame.root_draws == 0


def test_generated_root_cannot_be_used_as_a_normalized_chance_distribution():
    game = LargeGenerativeGame()
    root = SampledRoot("public-root", 0, lambda rng: (LargeState(0), 1.0, 1.0))
    with pytest.raises(ValueError, match="require sampled rollouts"):
        DepthLimitedGame(game, root, UniformPolicy())
    subgame = DepthLimitedGame(game, root, UniformPolicy(), sampled_rollouts=True)
    with pytest.raises(ValueError, match="initial-root proposal"):
        subgame.chance_outcomes(subgame.initial_state())


def test_generated_search_keeps_strict_continuation_coverage():
    game = KuhnPoker()
    target = game.replay((2, 0))
    root = build_public_root(game, game.public_key(target), UniformPolicy())
    with pytest.raises(SearchCoverageError, match="blueprint does not cover"):
        solve_generated_subgame(
            game,
            SampledRoot(root.public_key, 0, finite_sampler(root)),
            TabularPolicy(),
            target_player=0,
            target_information_key=game.information_key(target, 0),
            depth_limit=1,
        )


def test_cancellation_inside_root_generation_returns_no_result():
    cancelled = False

    def sample(rng):
        nonlocal cancelled
        cancelled = True
        return LargeState(0), 1.0, 1.0

    with pytest.raises(CancellationError):
        solve_generated_subgame(
            LargeGenerativeGame(),
            SampledRoot("public-root", 0, sample),
            UniformPolicy(),
            target_player=0,
            target_information_key="private:0",
            should_cancel=lambda: cancelled,
        )


def test_root_generation_uses_the_total_wall_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(search.time, "monotonic", lambda: now[0])

    def sample(rng):
        now[0] += 0.6
        return LargeState(0), 1.0, 1.0

    with pytest.raises(EvaluationLimitError, match="deadline"):
        solve_generated_subgame(
            LargeGenerativeGame(),
            SampledRoot("public-root", 0, sample),
            UniformPolicy(),
            target_player=0,
            target_information_key="private:0",
            deadline_seconds=1,
        )
