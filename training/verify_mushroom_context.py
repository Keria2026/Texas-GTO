"""Measure reusable eight-player contexts with the same game and Pluribus schedule."""

import argparse
import json
import resource
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic

from texas_game_core.mushroom_game import MushroomSessionGame
from texas_strategy.blueprint import CHECKPOINT_SCHEMA_VERSION
from texas_strategy.card_buckets import codebook
from texas_strategy.cfr import Trainer
from texas_strategy.mushroom_abstraction import MushroomContextGame


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=200)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    args.directory.mkdir(parents=True, exist_ok=True)
    checkpoint = args.directory / "checkpoint.json"
    if checkpoint.exists():
        parser.error("use a fresh benchmark directory to preserve previous checkpoints")
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, resource.RLIM_INFINITY))
    trainer = Trainer(MushroomContextGame(MushroomSessionGame()), algorithm="pluribus", seed=42)
    root = Path(__file__).resolve().parents[1]
    sources = (
        "engines/strategy/src/texas_strategy/mushroom_abstraction.py",
        "engines/strategy/src/texas_strategy/card_buckets.py",
        "engines/strategy/src/texas_strategy/cfr.py",
        "engines/strategy/src/texas_strategy/pluribus.py",
        "engines/strategy/src/texas_strategy/blueprint.py",
        "packages/game-core/src/texas_game_core/mushroom_game.py",
        "packages/game-core/src/texas_game_core/mushroom_table.py",
        "packages/game-core/src/texas_game_core/mushroom_hand.py",
        "packages/game-core/src/texas_game_core/holdem.py",
        "training/verify_mushroom_context.py",
    )
    source_hashes = {path: sha256((root / path).read_bytes()).hexdigest() for path in sources}
    started, rows = monotonic(), []
    for i in range(1, args.iterations + 1):
        diagnostics = trainer.train(1, max_nodes=1_000_000, deadline_seconds=60)
        assert diagnostics.completed_iterations == 1, diagnostics
        if i in (2, 10, 25, 50, 100, 147, args.iterations):
            trainer.save_checkpoint(checkpoint)
            row = {
                "iterations": i,
                "information_sets": len(trainer._table),
                "averaged_information_sets": sum(
                    any(e.strategy_sum) for e in trainer._table.values()
                ),
                "checkpoint_bytes": checkpoint.stat().st_size,
                "checkpoint_schema": CHECKPOINT_SCHEMA_VERSION,
                "elapsed_seconds": monotonic() - started,
                "stages": dict(sorted(Counter(trainer._betting_rounds.values()).items())),
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
            (args.directory / "progress.json").write_text(json.dumps(rows, indent=2))
    restored = Trainer.load_checkpoint(trainer.game, checkpoint)
    assert restored._table == trainer._table and restored._rng.getstate() == trainer._rng.getstate()
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "scope": "Eight seats at 800 points, two-hand approximation; no live quality claim",
        "seed": 42,
        "rules_version": trainer.game.rules_version,
        "card_codebook_checksum": codebook()[0],
        "source_sha256": source_hashes,
        "milestones": rows,
        "checkpoint_reload": "table_and_rng_equal",
    }
    (args.directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
