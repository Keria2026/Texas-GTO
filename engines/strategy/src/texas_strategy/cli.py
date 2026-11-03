"""Budgeted offline training, resume, evaluation and guarded model adaptation.

Run ``python -m texas_strategy.cli --help``. Research train/resume use legacy
JSON artifacts; checkpoint-backup exports portable SQLite training snapshots.
No platform connection, live cards, credentials or training service is required.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from math import isfinite
from pathlib import Path
from typing import Any

from texas_game_core.games import get_game
from texas_game_core.holdem import HoldemGame
from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import EvaluationLimitError, compare_policies, evaluate_strategy
from texas_strategy.policy import TabularPolicy


def _nonnegative(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def _positive(value: str) -> float:
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser(
        "checkpoint-backup", help="Export a portable SQLite checkpoint without loading its table"
    )
    backup.add_argument(
        "--source", type=Path, required=True, help="Source SQLite training database"
    )
    backup.add_argument("--output", type=Path, required=True, help="New standalone SQLite snapshot")
    backup.set_defaults(report=None)
    for name in ("train", "resume", "evaluate", "adapt"):
        command = commands.add_parser(name)
        command.add_argument(
            "--game", choices=("kuhn", "leduc", "holdem", "mushroom"), default="kuhn"
        )
        command.add_argument(
            "--session-hands", type=int, help="Explicit mushroom training horizon (default 2)"
        )
        command.add_argument("--players", type=int, default=2)
        command.add_argument(
            "--abstraction",
            choices=("none", "suit_canonical", "mushroom_context", "mushroom_population"),
            default="none",
        )
        command.add_argument("--stacks", help="Hold'em integer starting stacks, e.g. 4,4,4")
        command.add_argument("--small-blind", type=int, default=1)
        command.add_argument("--big-blind", type=int, default=2)
        command.add_argument("--straddle", type=int, help="Fully funded live straddle left of BB")
        command.add_argument(
            "--ante", type=_nonnegative, default=0, help="Per-player ante before blinds"
        )
        command.add_argument("--button", type=int, default=0)
        command.add_argument(
            "--bet-fractions", default="0.5,1.0", help="Hold'em pot fractions; use 'none' for none"
        )
        command.add_argument("--max-nodes", type=_nonnegative, default=1_000_000)
        command.add_argument("--seconds", type=_positive, default=30.0)
        command.add_argument("--report", type=Path, help="Also save the stdout JSON report here")
        command.add_argument("--blueprint", type=Path, required=True)
        if name in ("train", "resume"):
            command.add_argument("--iterations", type=_nonnegative, default=1000)
            command.add_argument("--checkpoint", type=Path, help="Save legacy JSON research state")
        if name == "train":
            command.add_argument("--algorithm", choices=("cfr", "mccfr", "pluribus"), default="cfr")
            command.add_argument("--weighting", choices=("uniform", "linear"), default="linear")
            command.add_argument("--seed", type=int, default=0)
            command.add_argument("--exploration", type=float, default=None)
            command.add_argument(
                "--pluribus-config",
                type=json.loads,
                help="JSON iteration schedule and regret thresholds for Pluribus",
            )
        elif name == "resume":
            command.add_argument(
                "--from-checkpoint", type=Path, required=True, help="Legacy JSON checkpoint"
            )
        elif name == "evaluate":
            command.add_argument("--mode", choices=("exact", "sampled"), default="exact")
            command.add_argument("--baseline", type=Path, help="Sampled comparison baseline")
            command.add_argument("--samples", type=_nonnegative, default=1000)
            command.add_argument("--seed", type=int, default=0)
            command.add_argument("--player", type=int, default=0)
            command.add_argument(
                "--require-coverage", action="store_true", help="Reject any untrained policy lookup"
            )
        else:
            command.add_argument(
                "--opponent-mode", choices=("call", "raise", "fold", "blueprint"), default="call"
            )
            command.add_argument("--hero", type=int, default=0)
            command.add_argument(
                "--synthetic-samples",
                type=_nonnegative,
                default=0,
                help="Assumed sample support for this simulation, not real observed players",
            )
            command.add_argument("--min-samples", type=_nonnegative, default=50)
            command.add_argument("--worst-case-loss", type=float, default=0.05)
            command.add_argument("--mixing-weights", default="0.05,0.1,0.25")
            command.add_argument("--require-coverage", action="store_true")
    return parser


def _game(args: argparse.Namespace) -> Any:
    if args.game == "mushroom":
        from texas_game_core.mushroom_game import MushroomSessionGame

        if (args.small_blind, args.big_blind, args.straddle) != (2, 4, 8):
            raise ValueError("Mushroom model requires --small-blind 2 --big-blind 4 --straddle 8")
        if (
            args.abstraction not in ("none", "mushroom_context", "mushroom_population")
            or args.bet_fractions != "0.5,1.0"
        ):
            raise ValueError("Mushroom abstraction and action sizes use the current model defaults")
        if args.abstraction == "mushroom_population":
            from texas_strategy.mushroom_population import MushroomPopulationGame

            if args.players != 8 or args.stacks is not None or args.button != 0:
                raise ValueError("Population requires eight model seats and its declared openings")
            game = MushroomPopulationGame.default(horizon=args.session_hands or 2, ante=args.ante)
            if args.command == "resume":
                from texas_strategy.blueprint import DEFAULT_MAX_BYTES, _read

                document = _read(
                    args.from_checkpoint, kind="checkpoint", max_bytes=DEFAULT_MAX_BYTES
                )
                saved = MushroomPopulationGame.from_context(document.get("training_context"))
                if saved.horizon != game.horizon or saved.ante != game.ante:
                    raise ValueError(
                        "Population checkpoint horizon or ante does not match the requested game"
                    )
                return saved
            return game
        stacks = (
            (800,) * args.players
            if args.stacks is None
            else tuple(map(int, args.stacks.split(",")))
        )
        if len(stacks) != args.players:
            raise ValueError("--stacks must match --players")
        game = MushroomSessionGame(
            stacks,
            button=args.button,
            horizon=args.session_hands if args.session_hands is not None else 2,
            ante=args.ante,
        )
        if args.abstraction == "mushroom_context":
            from texas_strategy.mushroom_abstraction import MushroomContextGame

            return MushroomContextGame(game)
        return game
    if args.abstraction in ("mushroom_context", "mushroom_population"):
        raise ValueError("Mushroom abstractions apply only to mushroom sessions")
    if args.session_hands is not None:
        raise ValueError("--session-hands applies only to mushroom")
    if args.game != "holdem":
        if args.ante:
            raise ValueError("--ante is available only for Hold'em or mushroom")
        if args.abstraction != "none":
            raise ValueError("Suit canonicalization is available only for Hold'em")
        if args.stacks is not None:
            raise ValueError("--stacks is available only for Hold'em")
        if args.straddle is not None:
            raise ValueError("--straddle is available only for Hold'em")
        if (args.small_blind, args.big_blind, args.button, args.bet_fractions) != (
            1,
            2,
            0,
            "0.5,1.0",
        ):
            raise ValueError(
                "Blind, button and bet-fraction options are available only for Hold'em"
            )
        return get_game(args.game, args.players)
    stacks = (4,) * args.players if args.stacks is None else tuple(map(int, args.stacks.split(",")))
    if len(stacks) != args.players:
        raise ValueError("--stacks must have one integer for each --players seat")
    fractions = (
        ()
        if args.bet_fractions == "none"
        else tuple(float(value) for value in args.bet_fractions.split(","))
    )
    game = HoldemGame(
        stacks,
        small_blind=args.small_blind,
        big_blind=args.big_blind,
        straddle=args.straddle,
        ante=args.ante,
        button=args.button,
        bet_fractions=fractions,
    )
    if args.abstraction == "suit_canonical":
        from texas_strategy.abstraction import SuitCanonicalHoldem

        return SuitCanonicalHoldem(game)
    return game


def _validate_paths(args: argparse.Namespace) -> None:
    outputs = []
    if args.command in ("train", "resume"):
        outputs.append(args.blueprint.resolve())
        if args.checkpoint is not None:
            outputs.append(args.checkpoint.resolve())
    if args.report is not None:
        outputs.append(args.report.resolve())
    if len(outputs) != len(set(outputs)):
        raise ValueError("Blueprint, checkpoint and report output paths must be distinct")
    if args.command in ("evaluate", "adapt") and args.report is not None:
        inputs = [args.blueprint.resolve()]
        if getattr(args, "baseline", None) is not None:
            inputs.append(args.baseline.resolve())
        if args.report.resolve() in inputs:
            raise ValueError("Report cannot overwrite an input blueprint")
    if args.command == "resume":
        source = args.from_checkpoint.resolve()
        if args.blueprint.resolve() == source or (
            args.report is not None and args.report.resolve() == source
        ):
            raise ValueError("Blueprint or report cannot overwrite the input checkpoint")


def _coverage(policy: TabularPolicy) -> dict[str, int | float | None]:
    queries = policy.covered_queries + policy.fallback_count
    return {
        "saved_information_sets": len(policy.entries),
        "covered_queries": policy.covered_queries,
        "uniform_fallback_queries": policy.fallback_count,
        "observed_coverage_fraction": policy.covered_queries / queries if queries else None,
    }


def _warnings(game: Any) -> list[str]:
    warnings = [
        "Research implementation; not a reproduction of full Pluribus or a live recommendation.",
        "Untrained information sets use explicit uniform fallback unless coverage is required.",
    ]
    if game.num_players > 2:
        warnings.append("Multiplayer CFR training has no general Nash convergence guarantee.")
    if game.game_id == "holdem":
        warnings.append(
            "Exact private-card information creates sparse tabular coverage; "
            "this run is a budget/throughput experiment, not a usable Hold'em strategy."
        )
    if game.game_id == "mushroom":
        warnings.append(
            "Finite-horizon mushroom model refunds remaining contributions at its horizon; "
            "live-session continuation, re-straddle and strategy strength remain unverified."
        )
        if getattr(game, "abstraction_version", None) == "mushroom-context-v1":
            warnings.append(
                "Lossy card/chip contexts forget completed hands; "
                "matched policies remain approximate."
            )
    return warnings


def _train(args: argparse.Namespace, game: Any) -> dict[str, Any]:
    if args.command == "resume":
        trainer = Trainer.load_checkpoint(game, args.from_checkpoint)
    else:
        trainer = Trainer(
            game,
            algorithm=args.algorithm,
            weighting=args.weighting,
            seed=args.seed,
            exploration=args.exploration,
            pluribus=args.pluribus_config,
        )
    if not getattr(game, "supports_exact_enumeration", True) and trainer.algorithm == "cfr":
        raise ValueError(
            "Full card games require --algorithm mccfr or pluribus; full-tree CFR is not supported"
        )
    diagnostics = trainer.train(
        args.iterations, max_nodes=args.max_nodes, deadline_seconds=args.seconds
    )
    blueprint = Blueprint.from_trainer(trainer)
    blueprint.save(args.blueprint)
    if args.checkpoint is not None:
        trainer.save_checkpoint(args.checkpoint)
    warnings = _warnings(game)
    if trainer.iterations == 0:
        warnings.append("No complete iteration exists; the saved blueprint is entirely untrained.")
    return {
        "schema_version": 1,
        "command": args.command,
        "metadata": blueprint.metadata,
        "training": diagnostics.to_dict(),
        "requested_iterations": args.iterations,
        "budget": {"max_nodes": args.max_nodes, "seconds": args.seconds},
        "blueprint": str(args.blueprint),
        "blueprint_checksum": blueprint.checksum,
        "blueprint_bytes": args.blueprint.stat().st_size,
        "checkpoint": str(args.checkpoint) if args.checkpoint is not None else None,
        "coverage": _coverage(blueprint.policy),
        "warnings": warnings,
    }


def _load_policy(path: Path, game: Any, require_coverage: bool) -> tuple[Blueprint, TabularPolicy]:
    blueprint = Blueprint.load(path, game=game)
    return blueprint, TabularPolicy(
        blueprint.policy.entries, missing="error" if require_coverage else "uniform"
    )


def _evaluate(args: argparse.Namespace, game: Any) -> dict[str, Any]:
    blueprint, policy = _load_policy(args.blueprint, game, args.require_coverage)
    report = {
        "schema_version": 1,
        "command": "evaluate",
        "mode": args.mode,
        "metadata": blueprint.metadata,
        "blueprint_checksum": blueprint.checksum,
        "budget": {"max_nodes": args.max_nodes, "seconds": args.seconds},
        "warnings": _warnings(game),
    }
    if args.mode == "exact":
        if not getattr(game, "supports_exact_enumeration", True):
            raise ValueError(
                "Full card-game exact evaluation is not supported; select --mode sampled"
            )
        if args.baseline is not None:
            raise ValueError("--baseline is used only with --mode sampled")
        report["evaluation"] = evaluate_strategy(
            game, policy, max_nodes=args.max_nodes, deadline_seconds=args.seconds
        ).to_dict()
    else:
        baseline = (
            TabularPolicy()
            if args.baseline is None
            else _load_policy(args.baseline, game, args.require_coverage)[1]
        )
        report["baseline_source"] = "uniform" if args.baseline is None else str(args.baseline)
        report["evaluation"] = asdict(
            compare_policies(
                game,
                baseline,
                policy,
                player=args.player,
                samples=args.samples,
                seed=args.seed,
                max_nodes=args.max_nodes,
                deadline_seconds=args.seconds,
            )
        )
        report["warnings"].append(
            "Sampled gain is against the fixed baseline opponents at one seat; "
            "standard error covers paired sampling only, not worst-case exploitability."
        )
    report["coverage"] = _coverage(policy)
    return report


def _adapt(args: argparse.Namespace, game: Any) -> dict[str, Any]:
    from texas_strategy.adaptation import adapt_strategy
    from texas_strategy.search import ContinuationPolicy

    blueprint, policy = _load_policy(args.blueprint, game, args.require_coverage)
    opponent = ContinuationPolicy(policy, args.opponent_mode)
    model_version = f"synthetic-continuation-{args.opponent_mode}-v1"
    result = adapt_strategy(
        game,
        policy,
        opponent,
        hero=args.hero,
        max_worst_case_loss=args.worst_case_loss,
        mixing_weights=tuple(map(float, args.mixing_weights.split(","))),
        sample_count=args.synthetic_samples,
        min_samples=args.min_samples,
        max_nodes=args.max_nodes,
        deadline_seconds=args.seconds,
        model_version=model_version,
    )
    return {
        "schema_version": 1,
        "command": "adapt",
        "metadata": blueprint.metadata,
        "blueprint_checksum": blueprint.checksum,
        "opponent_source": "synthetic_continuation_policy",
        "opponent_mode": args.opponent_mode,
        "synthetic_sample_support": args.synthetic_samples,
        "budget": {"max_nodes": args.max_nodes, "seconds": args.seconds},
        "adaptation": result.to_dict(),
        "coverage": _coverage(policy),
        "warnings": [
            *_warnings(game),
            "Opponent behavior and sample support are declared simulation inputs; "
            "they are not fitted or measured from a real player profile.",
            "Model gain is conditional on that synthetic opponent; the two-player "
            "guard limits measured worst-case loss relative to this baseline only.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "checkpoint-backup":
            from texas_strategy.training_store import backup_checkpoint

            backup_checkpoint(args.source, args.output)
            report = {
                "schema_version": 1,
                "command": args.command,
                "checkpoint": str(args.output),
                "checkpoint_bytes": args.output.stat().st_size,
            }
        else:
            _validate_paths(args)
            game = _game(args)
            if args.command == "evaluate":
                report = _evaluate(args, game)
            elif args.command == "adapt":
                report = _adapt(args, game)
            else:
                report = _train(args, game)
        serialized = json.dumps(report, indent=2, sort_keys=True, allow_nan=False)
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(serialized + "\n", encoding="utf-8")
        print(serialized)
        return 0
    except (ValueError, KeyError, OSError, EvaluationLimitError) as error:
        print(json.dumps({"error": str(error), "command": args.command}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
