"""One resumable, idle-only Pluribus worker, isolated from the HTTP process.

The candidate is a finite-horizon research model, never an automatically
approved live policy. Pausing preserves an in-flight traversal; stopping rolls
it back to the last complete database transaction.
"""

import asyncio
import fcntl
import logging
import multiprocessing
import os
import resource
import sqlite3
from contextlib import closing, contextmanager
from math import isfinite
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep
from uuid import uuid4

from texas_strategy.blueprint import DEFAULT_MAX_BYTES, _canonical, _read
from texas_strategy.mushroom_population import MAX_ROOTS, MushroomPopulationGame, _root
from texas_strategy.training_store import _header, open_trainer

from . import capture_openings
from .game_factory import make_game
from .models import CoreTrainingResponse, GameConfig

CORE_GAME = GameConfig(
    game_id="mushroom",
    abstraction="mushroom_population",
    num_players=8,
    stack=800,
    small_blind=2,
    big_blind=4,
    straddle=8,
    ante=4,
    session_hands=2,
)
LEGACY_CORE_GAME = CORE_GAME.model_copy(update={"abstraction": "mushroom_context", "ante": 0})
MEMORY_LIMIT = 1024 * 1024 * 1024
MAX_RECOVERIES = 3


def _open_core_trainer(directory, *, checkpoint_limit=DEFAULT_MAX_BYTES):
    trainer = _restore_core_trainer(directory, checkpoint_limit=checkpoint_limit)
    try:
        if trainer.game.ante == 0:
            trainer._store.backup(
                Path(directory) / f"before-ante-{trainer.iterations}-{uuid4().hex[:8]}.sqlite3"
            )
            trainer._store.enable_ante_training(
                trainer, trainer.game.with_ante(source_iterations=trainer.iterations)
            )
        if trainer.pluribus.average_mode == "sampled_uniform":
            trainer._store.backup(
                Path(directory) / f"before-mixture-{trainer.iterations}-{uuid4().hex[:8]}.sqlite3"
            )
            trainer._store.enable_mixed_average(trainer)
        return trainer
    except BaseException:
        trainer._store.close()
        raise


def _restore_core_trainer(directory, *, checkpoint_limit=DEFAULT_MAX_BYTES):
    """Resume the frozen distribution, or back up and explicitly warm-start the old core."""
    directory = Path(directory)
    database, checkpoint = directory / "training.sqlite3", directory / "checkpoint.json"
    legacy = make_game(LEGACY_CORE_GAME)
    if database.exists() or database.is_symlink():
        try:
            with closing(
                sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True)
            ) as db:
                document = _header(db, full_check=False)
        except sqlite3.Error as error:
            raise ValueError(f"Invalid or unavailable core training database: {error}") from error
        context = document["training_context"]
        if context != legacy.checkpoint_context:
            game = MushroomPopulationGame.from_context(context)
            if game.horizon != CORE_GAME.session_hands or (
                game.warm_start is not None
                and game.warm_start["source_iterations"] > document["metadata"]["iterations"]
            ):
                raise ValueError(
                    "Population checkpoint does not match the core horizon or provenance"
                )
            return open_trainer(game, database, max_bytes=checkpoint_limit)
    elif not (checkpoint.exists() or checkpoint.is_symlink()):
        return open_trainer(make_game(CORE_GAME), database, max_bytes=checkpoint_limit)
    else:
        document = _read(checkpoint, kind="checkpoint", max_bytes=checkpoint_limit)
        context = document.get("training_context")
        if context not in (None, legacy.checkpoint_context):
            game = MushroomPopulationGame.from_context(context)
            if game.horizon != CORE_GAME.session_hands or (
                game.warm_start is not None
                and game.warm_start["source_iterations"] > document["metadata"]["iterations"]
            ):
                raise ValueError(
                    "Population checkpoint does not match the core horizon or provenance"
                )
            return open_trainer(game, database, checkpoint=checkpoint, max_bytes=checkpoint_limit)
    trainer = open_trainer(legacy, database, checkpoint=checkpoint, max_bytes=checkpoint_limit)
    try:
        population = MushroomPopulationGame.from_legacy(
            legacy, source_iterations=trainer.iterations
        )
        # A failed attempt never replaces a previous rollback artifact.
        trainer._store.backup(
            directory / f"before-population-{trainer.iterations}-{uuid4().hex[:8]}.sqlite3"
        )
        trainer._store.enable_population_training(trainer, population)
        return trainer
    except BaseException:
        trainer._store.close()
        raise


def _incorporate_openings(trainer, directory):
    """Only change the curriculum between fully committed iterations."""
    path = Path(directory).parent / "capture.sqlite3"
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA cache_size=-8192")
        db.execute("PRAGMA mmap_size=0")
        db.execute("BEGIN")
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_training_openings'"
        ).fetchone():
            return False
        roots, limited = capture_openings.load(db)
    known = {
        _canonical({k: v for k, v in root.items() if k != "weight"}) for root in trainer.game.roots
    }
    added = []
    for value in roots:
        root = _root(value)
        if root["weight"] != 1:
            raise ValueError("Captured openings must have neutral weights")
        key = _canonical({k: v for k, v in root.items() if k != "weight"})
        if key in known:
            continue
        if len(known) >= MAX_ROOTS:
            limited = True
            continue
        known.add(key)
        added.append(root)
    if added:
        target = trainer.game.with_openings(added, source_iterations=trainer.iterations)
        if trainer.game.opening_updates is None:
            trainer._store.backup(
                Path(directory) / f"before-openings-{trainer.iterations}-{uuid4().hex[:8]}.sqlite3"
            )
        trainer._store.enable_population_training(trainer, target)
    return limited


class _Flag:
    """Independent single-byte signals cannot inherit a lock from a killed worker."""

    def __init__(self, initial=False):
        self._value = multiprocessing.RawValue("b", initial)

    def is_set(self):
        return bool(self._value.value)

    def set(self):
        self._value.value = 1

    def clear(self):
        self._value.value = 0


def _worker(directory, permission, stopped, parent_pid, checkpoint_limit):
    directory = Path(directory)
    with (directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(73) from None
        _train(directory, permission, stopped, parent_pid, checkpoint_limit)


def _train(directory, permission, stopped, parent_pid, checkpoint_limit):
    status = CoreTrainingResponse(
        enabled=True, phase="starting", worker_pid=os.getpid(), game=CORE_GAME
    )
    progress = directory / "progress.json"
    store = None

    def publish(phase, *, reason=None, force=False):
        if not force and status.phase == phase and status.reason == reason:
            return
        status.phase, status.reason = phase, reason
        temporary = progress.with_suffix(".tmp")
        temporary.write_text(status.model_dump_json(), encoding="utf-8")
        os.replace(temporary, progress)

    def stopping():
        return stopped.is_set() or os.getppid() != parent_pid

    def cooperate():
        while not stopping():
            if permission.is_set():
                publish("training")
                return False
            publish("paused" if status.iterations else "waiting_for_idle")
            sleep(0.1)
        return True

    def record_saved(trainer):
        status.iterations = trainer.iterations
        migration = trainer.game.rule_migration
        status.rule_iterations = trainer.iterations - (
            migration["source_iterations"] if migration else 0
        )
        status.information_sets, status.averaged_information_sets = store.counts()
        status.checkpoint_bytes = store.bytes_on_disk()
        status.training_roots = len(trainer.game.roots)
        status.observed_openings = trainer.game.observed_opening_count

    try:
        os.nice(10)
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = min(v for v in (soft, hard, MEMORY_LIMIT) if v != resource.RLIM_INFINITY)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
        publish("starting", force=True)
        trainer = _open_core_trainer(directory, checkpoint_limit=checkpoint_limit)
        store = trainer._store
        if trainer.algorithm != "pluribus":
            raise ValueError("Core checkpoint must use Pluribus")
        record_saved(trainer)
        publish("waiting_for_idle", force=True)
        while not cooperate():
            status.opening_capacity_reached = _incorporate_openings(trainer, directory)
            # No wall deadline: foreground pauses must not discard a long iteration.
            diagnostics = trainer.train(1, should_cancel=cooperate)
            if not diagnostics.completed_iterations:
                break
            publish("saving")
            record_saved(trainer)
            publish("saving", force=True)
        publish("stopped")
    except MemoryError:
        publish("limited", reason="Worker memory limit; last saved checkpoint retained")
    except Exception as error:
        logging.exception("Core self-play stopped; saved checkpoint was not reset")
        publish("error", reason=f"{type(error).__name__}: {error}"[:500])
    finally:
        if store is not None:
            store.close()


class CoreTraining:
    def __init__(
        self,
        directory: Path,
        *,
        enabled: bool = True,
        idle_seconds: float = 30,
        checkpoint_limit: int = DEFAULT_MAX_BYTES,
    ):
        if not isfinite(idle_seconds) or idle_seconds < 0:
            raise ValueError("Idle interval must be finite and nonnegative")
        if type(checkpoint_limit) is not int or not 0 < checkpoint_limit <= DEFAULT_MAX_BYTES:
            raise ValueError("Legacy checkpoint import limit must be between 1 byte and 64 MiB")
        self.directory, self.enabled, self.idle_seconds = directory, enabled, idle_seconds
        self.checkpoint_limit = checkpoint_limit
        self._lock = Lock()
        self._last_activity, self._foreground = monotonic(), 0
        self._process = None
        self._recoveries, self._recover_at, self._recovery_reason = 0, None, None
        context = multiprocessing.get_context("spawn")
        self._permission, self._worker_stopped = _Flag(), _Flag()
        self._stopped = Event()
        self._context = context

    def start(self):
        with self._lock:
            if not self.enabled or self._stopped.is_set() or self._process is not None:
                return
            self._spawn()

    def _spawn(self):
        # The lifecycle lock also prevents close() from missing a newly spawned process.
        self.directory.mkdir(parents=True, exist_ok=True)
        permission, stopped = _Flag(self._permission.is_set()), _Flag()
        process = self._context.Process(
            target=_worker,
            args=(
                self.directory,
                permission,
                stopped,
                os.getpid(),
                self.checkpoint_limit,
            ),
            name="texas-core-training",
            daemon=True,
        )
        process.start()
        self._process = process
        self._permission, self._worker_stopped = permission, stopped

    @contextmanager
    def foreground(self):
        with self._lock:
            self._foreground += 1
            self._permission.clear()
        try:
            yield
        finally:
            with self._lock:
                self._foreground -= 1
                self._last_activity = monotonic()

    def allow_if_idle(self, research_busy=False):
        with self._lock:
            if (
                not self._stopped.is_set()
                and not research_busy
                and self._foreground == 0
                and monotonic() - self._last_activity >= self.idle_seconds
            ):
                self._permission.set()
            else:
                self._permission.clear()

    async def supervise(self, research_busy):
        while not self._stopped.is_set():
            self._recover()
            self.allow_if_idle(research_busy())
            await asyncio.sleep(0.1)

    def _progress(self):
        result = CoreTrainingResponse(enabled=self.enabled, game=CORE_GAME)
        if not self.enabled:
            return result
        result.phase = "starting"
        path = self.directory / "progress.json"
        if path.is_file():
            try:
                result = CoreTrainingResponse.model_validate_json(path.read_bytes())
            except (OSError, ValueError):
                result.phase, result.reason = "error", "Training status is unavailable"
        return result

    def _unexpected_exit(self, progress):
        process = self._process
        return (
            process is not None
            and process.exitcode not in (None, 73)
            and not (
                progress.worker_pid == process.pid
                and progress.phase in ("limited", "error", "stopped")
            )
        )

    def _recover(self):
        with self._lock:
            if not self.enabled or self._stopped.is_set():
                return
            if not self._unexpected_exit(self._progress()) or self._recoveries >= MAX_RECOVERIES:
                return
            if self._recover_at is None:
                delay = 2**self._recoveries
                self._recover_at = monotonic() + delay
                self._recovery_reason = (
                    f"Unexpected worker exit; recovery {self._recoveries + 1}/{MAX_RECOVERIES} "
                    f"in {delay}s; saved checkpoint retained"
                )
            elif monotonic() >= self._recover_at:
                self._process.join(timeout=0)
                self._recoveries += 1
                self._recover_at = None
                self._recovery_reason = (
                    f"Automatic recovery {self._recoveries}/{MAX_RECOVERIES}; "
                    "restoring saved checkpoint"
                )
                try:
                    self._spawn()
                except Exception as error:
                    logging.exception("Could not restart training worker; checkpoint retained")
                    self._recovery_reason = (
                        f"Recovery {self._recoveries}/{MAX_RECOVERIES} failed to start: "
                        f"{type(error).__name__}; saved checkpoint retained"
                    )

    def status(self):
        with self._lock:
            result = self._progress()
            if not self.enabled:
                return result
            process = self._process
            if self._stopped.is_set() and (process is None or process.exitcode is not None):
                result.phase, result.reason = "stopped", "Worker stopped; saved checkpoint retained"
                return result
            unexpected = self._unexpected_exit(result)
            if process is None or result.worker_pid != process.pid:
                result.phase, result.worker_pid = "starting", None
            if process is None:
                return result
            if process.exitcode == 73:
                result.phase, result.reason = "error", "Another worker owns this core directory"
            elif unexpected:
                if self._recoveries >= MAX_RECOVERIES:
                    result.phase, result.reason = (
                        "error",
                        f"Worker recovery limit reached ({MAX_RECOVERIES}/{MAX_RECOVERIES}); "
                        "saved checkpoint retained",
                    )
                else:
                    result.phase, result.reason = (
                        "starting",
                        (
                            self._recovery_reason
                            if self._recover_at is not None
                            else f"Unexpected worker exit; awaiting automatic recovery "
                            f"{self._recoveries + 1}/{MAX_RECOVERIES}"
                        ),
                    )
            elif self._recoveries and result.reason is None:
                result.reason = (
                    self._recovery_reason
                    if result.phase == "starting"
                    else f"Automatic recovery completed ({self._recoveries}/{MAX_RECOVERIES})"
                )
            return result

    def close(self):
        with self._lock:
            self._stopped.set()
            self._permission.clear()
            self._worker_stopped.set()
            self._recover_at = None
            process = self._process
        if process is not None:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
