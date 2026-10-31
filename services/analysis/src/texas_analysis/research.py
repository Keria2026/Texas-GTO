"""Bounded local research jobs and explicit blueprint coverage."""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from uuid import UUID, uuid4

from texas_strategy.blueprint import Blueprint
from texas_strategy.cfr import Trainer
from texas_strategy.evaluation import EvaluationLimitError, evaluate_strategy
from texas_strategy.provider import PluribusStyleProvider
from texas_strategy.search import SearchCoverageError, solve_subgame

from .game_factory import make_game
from .models import (
    ActionFrequency,
    AnalysisResponse,
    AnalyzeRequest,
    GameConfig,
    JobResponse,
    TrainRequest,
)
from .storage import ConflictError, SessionStore


class QueueFullError(ValueError):
    pass


class ResearchManager:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="texas-research")
        self._jobs: dict[str, JobResponse] = {}
        self._cancel: dict[str, Event] = {}
        self._closed = False

    def _persist(self, job):
        destination = self.directory / job.job_id / "job.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(job.model_dump_json(), encoding="utf-8")
        os.replace(temporary, destination)

    def submit(self, request: TrainRequest):
        with self._lock:
            if (
                self._closed
                or sum(j.status in ("queued", "running") for j in self._jobs.values()) >= 4
            ):
                raise QueueFullError("Research queue is full; wait or cancel a job")
            job = JobResponse(job_id=str(uuid4()), status="queued")
            self._jobs[job.job_id] = job
            self._cancel[job.job_id] = Event()
            self._persist(job)
            result = job.model_copy(deep=True)
            self._pool.submit(self._run, job.job_id, request)
            return result

    def _run(self, job_id, request):
        cancelled = self._cancel[job_id]
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
        try:
            game = make_game(request.game)
            trainer = Trainer(
                game, algorithm=request.algorithm, weighting=request.weighting, seed=request.seed
            )
            diagnostics = trainer.train(
                request.iterations,
                max_nodes=request.max_nodes,
                deadline_seconds=request.deadline_seconds,
                should_cancel=cancelled.is_set,
            )
            evaluation = None
            blueprint_id = None
            if trainer.iterations > 0:
                blueprint = Blueprint.from_trainer(trainer)
                target = self.directory / job_id
                blueprint.save(target / "blueprint.json")
                trainer.save_checkpoint(target / "checkpoint.json")
                (target / "game.json").write_text(request.game.model_dump_json(), encoding="utf-8")
                blueprint_id = job_id
                if game.game_id == "holdem":
                    evaluation = {
                        "quality": "not_evaluated",
                        "reason": "Full Holdem exact evaluation exceeds the service budget",
                    }
                elif not cancelled.is_set():
                    try:
                        evaluation = evaluate_strategy(
                            game, blueprint.policy, max_nodes=100_000, deadline_seconds=3
                        ).to_dict()
                        evaluation["uniform_fallback_queries"] = blueprint.policy.fallback_count
                    except EvaluationLimitError as exc:
                        evaluation = {"quality": "not_evaluated", "reason": str(exc)}
            with self._lock:
                job.diagnostics = diagnostics.to_dict()
                job.blueprint_id = blueprint_id
                job.evaluation = evaluation
                job.status = "cancelled" if cancelled.is_set() else "completed"
                if trainer.iterations == 0:
                    job.error = (
                        "Budget allowed no complete iteration; no trained blueprint was saved"
                    )
        except Exception as exc:
            with self._lock:
                job.status = "cancelled" if cancelled.is_set() else "failed"
                # Only our validated synthetic game/solver errors; no request tokens or paths.
                job.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            with self._lock:
                self._persist(job)

    def job(self, job_id):
        UUID(job_id)
        with self._lock:
            if job_id in self._jobs:
                return self._jobs[job_id].model_copy(deep=True)
        path = self.directory / job_id / "job.json"
        if not path.is_file():
            raise KeyError("Unknown training job")
        job = JobResponse.model_validate_json(path.read_bytes())
        if job.status in ("queued", "running"):
            job.status = "cancelled"
            if (path.parent / "checkpoint.json").is_file():
                job.error = "Service restarted; resume the saved checkpoint using the research CLI"
            else:
                job.error = "Service restarted before a checkpoint was saved; start a new job"
        return job

    def cancel(self, job_id):
        UUID(job_id)
        with self._lock:
            if job_id in self._cancel and self._jobs[job_id].status in ("queued", "running"):
                self._cancel[job_id].set()
        return self.job(job_id)

    def blueprint(self, blueprint_id, game=None):
        UUID(blueprint_id)
        path = self.directory / blueprint_id / "blueprint.json"
        if not path.is_file():
            raise KeyError("Unknown blueprint")
        return Blueprint.load(path, game=game)

    def game(self, blueprint_id):
        UUID(blueprint_id)
        path = self.directory / blueprint_id / "game.json"
        if not path.is_file():
            raise KeyError("Blueprint game configuration is unavailable")
        return make_game(GameConfig.model_validate_json(path.read_bytes()))

    def blueprints(self):
        results = []
        for path in sorted(self.directory.glob("*/blueprint.json")):
            try:
                UUID(path.parent.name)
                blueprint = Blueprint.load(path)
            except ValueError:
                continue
            results.append(
                {
                    "blueprint_id": path.parent.name,
                    "metadata": blueprint.metadata,
                    "information_sets": len(blueprint.policy.entries),
                }
            )
        return {"blueprints": results}

    def close(self):
        with self._lock:
            self._closed = True
            for event in self._cancel.values():
                event.set()
        self._pool.shutdown(wait=True, cancel_futures=False)


def analyze(store: SessionStore, manager: ResearchManager, request: AnalyzeRequest):
    _, _, game, state, version = store.load(request.session_id)
    if version != request.state_version:
        raise ConflictError("Analysis state version is stale")
    blueprint = manager.blueprint(request.blueprint_id)
    response = AnalysisResponse(
        request_id=str(uuid4()),
        session_id=request.session_id,
        state_version=version,
        status="unsupported",
        strategy_source="none",
        rules_version=game.rules_version,
        model_version=blueprint.checksum,
        actions=[],
        warnings=[],
    )
    coverage = PluribusStyleProvider(blueprint).coverage(game.rules_version, game=game, state=state)
    if not coverage.supported:
        response.warnings = list(coverage.reasons)
        return response
    probabilities = blueprint.policy.probabilities(game, state)
    actions = tuple(game.legal_actions(state))
    response.strategy_source = "blueprint"
    response.warnings = ["Finite-budget research strategy; no global optimality guarantee"]
    if game.game_id == "holdem":
        response.warnings.append(
            "Sparse unabstracted Holdem experiment; not a trained six-player GTO model"
        )
    if request.search:
        if game.game_id == "holdem":
            response.warnings.append(
                "Full Holdem public-root enumeration unsupported; using covered blueprint"
            )
        else:
            try:
                # Exact root and rollout have their own bounded counts, all share one wall deadline.
                result = solve_subgame(
                    game,
                    state,
                    blueprint.policy,
                    depth_limit=request.depth_limit,
                    iterations=request.iterations,
                    max_root_nodes=min(request.max_nodes, 100_000),
                    max_rollout_nodes=request.max_nodes,
                    max_nodes=request.max_nodes,
                    deadline_seconds=request.deadline_seconds,
                )
                actions, probabilities = result.actions, result.probabilities
                response.strategy_source = "depth_limited_search"
                response.diagnostics = result.to_dict()
                response.warnings.extend(result.warnings)
            except (EvaluationLimitError, SearchCoverageError) as exc:
                response.warnings.append(f"Search unavailable; using covered blueprint: {exc}")
    response.actions = [
        ActionFrequency(action=a, frequency=p) for a, p in zip(actions, probabilities, strict=True)
    ]
    response.status = "ready"
    if store.load(request.session_id)[4] != version:
        raise ConflictError("State changed during analysis; discard this result")
    return response
