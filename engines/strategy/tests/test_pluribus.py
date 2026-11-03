"""Paper traversal rules, averaging, pruning recovery and transactional training."""

from dataclasses import asdict
from math import isfinite

import pytest

from texas_game_core.holdem import HoldemGame
from texas_game_core.kuhn import KuhnPoker
from texas_game_core.leduc import LeducPoker
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer, _TableEntry
from texas_strategy.evaluation import evaluate_strategy
from texas_strategy.pluribus import PluribusConfig
from texas_strategy.regret import regret_matching
from texas_strategy.search import solve_subgame


@pytest.mark.parametrize("abstraction", [False, True])
def test_eight_player_live_straddle_checkpoint_resume_and_rule_isolation(tmp_path, abstraction):
    from texas_strategy.abstraction import SuitCanonicalHoldem

    base = HoldemGame((16,) * 8, small_blind=2, big_blind=4, straddle=8, bet_fractions=())
    game = SuitCanonicalHoldem(base) if abstraction else base
    direct = Trainer(game, algorithm="pluribus", seed=42)
    assert direct.train(2, max_nodes=100_000, deadline_seconds=10).completed_iterations == 2
    first = Trainer(game, algorithm="pluribus", seed=42)
    assert first.train(1, max_nodes=100_000, deadline_seconds=10).completed_iterations == 1
    checkpoint = tmp_path / "eight-checkpoint.json"
    first.save_checkpoint(checkpoint)
    resumed = Trainer.load_checkpoint(game, checkpoint)
    assert resumed.train(1, max_nodes=100_000, deadline_seconds=10).completed_iterations == 1
    blueprint = Blueprint.from_trainer(resumed)
    assert blueprint.checksum == Blueprint.from_trainer(direct).checksum
    path = tmp_path / "eight-blueprint.json"
    blueprint.save(path)
    assert Blueprint.load(path, game=game).checksum == blueprint.checksum
    assert blueprint.metadata["num_players"] == 8
    assert {player for player, _key in blueprint.policy.entries} == set(range(8))
    other = HoldemGame((16,) * 8, small_blind=2, big_blind=4, bet_fractions=())
    if abstraction:
        other = SuitCanonicalHoldem(other)
    with pytest.raises(ValueError, match="incompatible"):
        Blueprint.load(path, game=other)


class TwoRoundGame:
    num_players = 2
    final_betting_round = 1
    game_id = "pruning-test"
    rules_version = "pruning-test-v1"

    def __init__(self):
        self.payoffs_seen = []

    def initial_state(self):
        return ()

    def actor(self, state):
        if not state:
            return 0
        if state[0] == "end" or len(state) == 3:
            return None
        return 1 if len(state) == 1 else 0

    def legal_actions(self, state):
        return (
            ("bad", "good", "end")
            if not state
            else (("pass",) if len(state) == 1 else ("up", "down"))
        )

    def betting_round(self, state):
        return int(len(state) >= 2)

    def active_players(self, state):
        return (0, 1)

    def information_key(self, state, player):
        return "root" if not state else str(state)

    def transition(self, state, action):
        return (*state, action)

    def terminal_payoffs(self, state):
        self.payoffs_seen.append(state)
        value = (
            0
            if state[0] == "end"
            else (20 if state[0] == "bad" else (1 if state[-1] == "up" else 2))
        )
        return value, -value


def pruning_trainer(seed=0):
    trainer = Trainer(
        TwoRoundGame(),
        algorithm="pluribus",
        seed=seed,
        pluribus={
            "prune_after": 0,
            "prune_threshold": -2.0,
            "regret_floor": -10.0,
            "discount_until": 0,
            "average_after": 10_000,
        },
    )
    trainer._table = {
        (0, "root"): _TableEntry(("bad", "good", "end"), [-8.0, 2.0, -8.0], [0.0] * 3),
        (0, "('good', 'pass')"): _TableEntry(("up", "down"), [-8.0, 2.0], [0.0] * 2),
    }
    trainer._betting_rounds = {(0, "root"): 0, (0, "('good', 'pass')"): 1}
    return trainer


def test_prunes_only_zero_mass_nonterminal_early_actions_and_preserves_river():
    trainer = pruning_trainer()
    diagnostics = trainer.train(1)
    assert diagnostics.pruned_actions == 1
    assert all(s[0] != "bad" for s in trainer.game.payoffs_seen)
    assert ("end",) in trainer.game.payoffs_seen  # Immediate payoff is never pruned.
    assert ("good", "pass", "up") in trainer.game.payoffs_seen  # Final round is never pruned.
    assert trainer._table[(0, "root")].regrets == [-8, 2, -10]
    assert trainer._table[(0, "('good', 'pass')")].regrets == [-9, 2]


def test_five_percent_full_traversal_recovers_a_previously_pruned_action():
    trainer = pruning_trainer(seed=31)  # First uniform draw is below .05.
    diagnostics = trainer.train(1)
    assert diagnostics.full_traversals >= 1
    assert diagnostics.pruned_actions == 0
    assert trainer._table[(0, "root")].regrets[0] > 0
    assert regret_matching(trainer._table[(0, "root")].regrets)[0] > 0


def test_all_negative_regrets_do_not_prune_positive_uniform_fallback_mass():
    trainer = pruning_trainer()
    trainer._table[(0, "root")].regrets = [-8.0] * 3
    assert trainer.train(1).pruned_actions == 0
    assert any(s[0] == "bad" for s in trainer.game.payoffs_seen)


def test_periodic_discount_scales_all_regrets_and_strategy_counts():
    trainer = pruning_trainer()
    trainer.pluribus = PluribusConfig(discount_interval=1, discount_until=2, average_after=1000)
    before = Trainer(
        trainer.game,
        algorithm="pluribus",
        seed=0,
        pluribus={"discount_until": 0, "average_after": 1000},
    )
    before._table = {
        k: _TableEntry(e.actions, e.regrets.copy(), [4.0] * len(e.actions))
        for k, e in trainer._table.items()
    }
    trainer._table = {
        k: _TableEntry(e.actions, e.regrets.copy(), e.strategy_sum.copy())
        for k, e in before._table.items()
    }
    before._betting_rounds = trainer._betting_rounds.copy()
    before.train(1)
    assert trainer.train(1).discounted_iterations == 1
    for key, entry in before._table.items():
        assert trainer._table[key].regrets == pytest.approx([v / 2 for v in entry.regrets])
        assert trainer._table[key].strategy_sum == pytest.approx(
            [v / 2 for v in entry.strategy_sum]
        )
    assert trainer.train(1).discounted_iterations == 0


def test_preflop_sampling_and_postflop_snapshots_are_separate_and_resume_exactly(tmp_path):
    game = LeducPoker()
    trainer = Trainer(
        game,
        algorithm="pluribus",
        seed=17,
        pluribus={"snapshot_interval": 5, "discount_interval": 2, "discount_until": 12},
    )
    trainer.train(4)
    postflop = [k for k in trainer._table if trainer._betting_rounds[k] == 1]
    assert postflop
    assert all(not any(trainer._table[k].strategy_sum) for k in postflop)
    assert any(
        any(e.strategy_sum) for k, e in trainer._table.items() if trainer._betting_rounds[k] == 0
    )
    path = tmp_path / "checkpoint.json"
    trainer.save_checkpoint(path)
    resumed = Trainer.load_checkpoint(game, path)
    assert asdict(resumed.pluribus) == asdict(trainer.pluribus)
    assert trainer.train(6).snapshot_iterations == resumed.train(6).snapshot_iterations == 2
    assert trainer._table == resumed._table
    assert trainer._betting_rounds == resumed._betting_rounds
    assert trainer._rng.getstate() == resumed._rng.getstate()
    assert any(any(trainer._table[k].strategy_sum) for k in postflop)


def test_abort_discards_averaging_regret_updates_rounds_and_rng():
    trainer = pruning_trainer()
    before = {
        k: _TableEntry(e.actions, e.regrets.copy(), e.strategy_sum.copy())
        for k, e in trainer._table.items()
    }
    rounds, rng = trainer._betting_rounds.copy(), trainer._rng.getstate()
    assert trainer.train(1, max_nodes=5).completed_iterations == 0
    assert trainer._table == before and trainer._betting_rounds == rounds
    assert trainer._rng.getstate() == rng
    assert trainer.train(1, deadline_seconds=0).stop_reason == "deadline"
    assert trainer.train(1, should_cancel=lambda: True).stop_reason == "cancelled"


class HiddenChoices:
    """Simultaneous binary choices: no player may observe earlier hidden choices."""

    game_id = "hidden-choices"
    final_betting_round = 0

    def __init__(self, players):
        self.num_players = players
        self.rules_version = f"hidden-choices-{players}p-v1"

    def initial_state(self):
        return ()

    def actor(self, state):
        return len(state) if len(state) < self.num_players else None

    def betting_round(self, state):
        return 0

    def active_players(self, state):
        return tuple(range(self.num_players))

    def legal_actions(self, state):
        return ("0", "1")

    def information_key(self, state, player):
        return f"seat-{player}"

    def transition(self, state, action):
        return (*state, action)

    def terminal_payoffs(self, state):
        return tuple(
            1.0 if state[p] == state[(p + 1) % self.num_players] else -1.0
            for p in range(self.num_players)
        )


@pytest.mark.parametrize("players", [4, 5, 6, 7, 8])
def test_four_to_eight_player_training_preserves_hidden_information_and_payoff_alignment(players):
    game = HiddenChoices(players)
    trainer = Trainer(game, algorithm="pluribus", seed=9)
    assert trainer.train(20, max_nodes=200_000).completed_iterations == 20
    policy = trainer.average_policy(missing="error")
    assert {p for p, _ in policy.entries} == set(range(players))
    for entry in policy.entries.values():
        assert all(isfinite(p) and 0 <= p <= 1 for p in entry.probabilities)
        assert sum(entry.probabilities) == pytest.approx(1)
    values = evaluate_strategy(game, policy, max_nodes=10_000)
    assert len(values.policy_values) == players
    assert values.exploitability is None


def test_blueprint_to_full_range_search_pipeline(tmp_path):
    game = KuhnPoker()
    trainer = Trainer(game, algorithm="pluribus", seed=42)
    trainer.train(3000)
    path = tmp_path / "blueprint.json"
    Blueprint.from_trainer(trainer).save(path)
    blueprint = Blueprint.load(path, game=game)
    result = solve_subgame(
        game, game.replay((2, 0)), blueprint.policy, depth_limit=3, iterations=600
    )
    assert result.root_worlds == 6
    assert result.blueprint_fallback_queries == 0
    assert evaluate_strategy(result.subgame, result.average_policy).exploitability < 0.025


def test_holdem_uses_exact_card_sampler_without_enumerating_chance_outcomes():
    class FastOnly(HoldemGame):
        def chance_outcomes(self, state):
            raise AssertionError("Must use the exact sampler, not materialize a full deck tree")

    game = FastOnly((4, 4), bet_fractions=())
    trainer = Trainer(game, algorithm="pluribus", seed=7, pluribus={"snapshot_interval": 1})
    diagnostics = trainer.train(3, max_nodes=100_000)
    assert diagnostics.completed_iterations == 3
    assert Blueprint.from_trainer(trainer).metadata["chance_sampling"] == "game_exact"
    assert trainer.average_policy().entries


@pytest.mark.parametrize(
    "config",
    [
        {"prune_after": True},
        {"strategy_interval": 0},
        {"snapshot_interval": -1},
        {"regret_floor": -1},
        {"prune_threshold": float("nan")},
        {"regret_floor": -(10**400)},
        {"unknown": 1},
    ],
)
def test_rejects_invalid_schedule_and_thresholds(config):
    with pytest.raises(ValueError):
        Trainer(KuhnPoker(), algorithm="pluribus", pluribus=config)
