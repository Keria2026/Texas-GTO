"""Reproduce the sampled-continuation checks; stdout is an offline JSON report."""

import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from time import monotonic

from texas_game_core.kuhn import KuhnPoker
from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import EvaluationLimitError, evaluate_strategy
from texas_strategy.search import RootDistribution, solve_subgame


class UniformPolicy:
    def probabilities(self, game, state):
        actions = game.legal_actions(state)
        return (1 / len(actions),) * len(actions)


def verify():
    game = KuhnPoker()
    trainer = Trainer(game, algorithm="pluribus", seed=42)
    assert trainer.train(3000).completed_iterations == 3000
    small = solve_subgame(
        game,
        game.replay((2, 0)),
        trainer.average_policy(missing="error"),
        depth_limit=1,
        iterations=3000,
        algorithm="mccfr",
        sampled_rollouts=True,
        seed=32,
        max_nodes=1_000_000,
        max_rollout_nodes=1_000_000,
    )
    small_search = small.to_dict()
    checked = evaluate_strategy(small.subgame, small.policy, max_nodes=100_000)
    assert small.blueprint_fallback_queries == 0 and checked.exploitability < 0.05
    assert abs(checked.policy_values[0] + 1 / 18) < 0.03

    game, rng = MushroomSessionGame(horizon=2), Random(8)
    worlds = []
    for _ in range(8):
        state = game.initial_state()
        while game.actor(state) == -1:
            action, _ = game.sample_chance_outcome(state, rng)
            state = game.transition(state, action)
        worlds.append(state)
    root = RootDistribution(game.public_key(worlds[0]), tuple(worlds), (1 / 8,) * 8, 1)
    large = solve_subgame(
        game,
        worlds[0],
        UniformPolicy(),
        root_distribution=root,
        sampled_rollouts=True,
        algorithm="mccfr",
        depth_limit=1,
        iterations=32,
        seed=3,
        max_nodes=500_000,
        max_rollout_nodes=500_000,
        deadline_seconds=30,
    )
    assert large.diagnostics.completed_iterations == 32
    assert abs(sum(large.probabilities) - 1) < 1e-10
    assert large.subgame.rollout_samples > 0 and large.subgame.rollout_cache_entries == 0
    started = monotonic()
    try:
        solve_subgame(
            game,
            worlds[0],
            UniformPolicy(),
            root_distribution=root,
            depth_limit=1,
            iterations=1,
            max_nodes=500_000,
            max_rollout_nodes=10_000,
            deadline_seconds=10,
        )
    except EvaluationLimitError as error:
        exact = {"status": "budget_exceeded", "reason": str(error)}
    else:
        raise AssertionError("The 10,000-node exact tail was unexpectedly sufficient")
    exact["elapsed_seconds"] = monotonic() - started
    root_dir = Path(__file__).resolve().parents[1]
    sources = (
        "engines/strategy/src/texas_strategy/search.py",
        "engines/strategy/src/texas_strategy/cfr.py",
        "engines/strategy/src/texas_strategy/pluribus.py",
        "packages/game-core/src/texas_game_core/holdem.py",
        "packages/game-core/src/texas_game_core/mushroom_game.py",
        "training/verify_sampled_search.py",
    )
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "source_sha256": {
            path: hashlib.sha256((root_dir / path).read_bytes()).hexdigest() for path in sources
        },
        "scope": "Offline engine verification; no live WPK data or strategy-strength claim",
        "kuhn": {
            "blueprint": {"algorithm": "pluribus", "iterations": 3000, "seed": 42},
            "search": small_search,
            "independent_exact_subgame_evaluation": checked.to_dict(),
        },
        "eight_player_mushroom": {
            "initial_stacks": [800] * 8,
            "horizon": 2,
            "root": "Eight synthetic hidden worlds, not a certified full public belief",
            "continuation": "Synthetic uniform policy, not a trained or deployable blueprint",
            "search": large.to_dict(),
            "exact_tail_reference_10000_node_limit": exact,
        },
    }


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
