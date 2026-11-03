"""Loopback-only research API. Synthetic gameplay and separate TGTO metadata ingestion."""

import asyncio
import hmac
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path
from threading import BoundedSemaphore, Event
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from texas_game_core.cards import estimate_equity
from texas_game_core.hand_analysis import analyze_hand_strength
from texas_strategy.adaptation import adapt_strategy
from texas_strategy.evaluation import CancellationError
from texas_strategy.search import ContinuationPolicy

from .core_training import CoreTraining
from .models import (
    AdaptationRequest,
    AnalysisResponse,
    AnalyzeRequest,
    CoreTrainingResponse,
    CreateSession,
    EquityRequest,
    HandStrengthRequest,
    HealthResponse,
    JobResponse,
    SessionResponse,
    SubmitAction,
    TrainRequest,
)
from .network_stream import NetworkStore, network_stream
from .research import QueueFullError, ResearchManager, analyze
from .storage import ConflictError, SessionStore

DEFAULT_ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5174",
)
EXTENSION_ORIGIN = r"chrome-extension://[a-p]{32}"
MAX_BODY_BYTES = 256 * 1024


class RollbackBehavior(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")


class LocalBoundary:
    """ASGI boundary checks real streamed bytes, not just Content-Length."""

    def __init__(self, app, *, origins, token, foreground=None):
        self.app, self.origins, self.token = app, origins, token
        self.foreground = foreground

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        origin = headers.get(b"origin")

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

        if origin is not None:
            value = origin.decode("latin1")
            if value not in self.origins and not (
                self.token is None and re.fullmatch(EXTENSION_ORIGIN, value)
            ):
                return await reject(403, "Origin is not allowed")
        public = scope["path"] == "/v1/health" and scope["method"] == "GET"
        if not public and scope["method"] != "OPTIONS":
            if self.token is None:
                # A fixed client marker forces browser preflight; it is not a credential.
                if headers.get(b"x-texas-local") != b"1":
                    return await reject(403, "Local client header is required")
            else:
                expected = f"Bearer {self.token}".encode()
                if not hmac.compare_digest(headers.get(b"authorization", b""), expected):
                    return await reject(401, "A valid bearer token is required")
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                size = int(content_length)
            except ValueError:
                return await reject(400, "Invalid Content-Length")
            if size < 0:
                return await reject(400, "Invalid Content-Length")
            if size > MAX_BODY_BYTES:
                return await reject(413, "Request exceeds 256 KiB")
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES:
                return await reject(413, "Request exceeds 256 KiB")
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        async def uncached(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"cache-control", b"no-store")]
            await send(message)

        activity = (
            self.foreground()
            if self.foreground is not None and scope["method"] in ("POST", "DELETE")
            else nullcontext()
        )
        with activity:
            await self.app(scope, replay, uncached)


def create_app(
    allowed_origins: tuple[str, ...] | None = None,
    *,
    token: str | None = None,
    data_dir: str | Path | None = None,
    auto_train: bool | None = None,
) -> FastAPI:
    if allowed_origins is None:
        configured = os.getenv("TEXAS_ALLOWED_ORIGINS")
        allowed_origins = (
            tuple(origin.strip() for origin in configured.split(",") if origin.strip())
            if configured is not None
            else DEFAULT_ORIGINS
        )
    if any("*" in origin or origin == "null" for origin in allowed_origins):
        raise ValueError("Origins must be explicit; wildcards and null are not allowed")
    token = token if token is not None else os.getenv("TEXAS_API_TOKEN") or None
    if token is not None and (
        len(token) < 24 or not token.isascii() or any(c.isspace() for c in token)
    ):
        raise ValueError("Local token must contain at least 24 ASCII characters without whitespace")
    directory = Path(data_dir or os.getenv("TEXAS_DATA_DIR", "artifacts/local"))
    if auto_train is None:
        configured = os.getenv("TEXAS_AUTO_TRAIN", "1")
        if configured not in ("0", "1"):
            raise ValueError("TEXAS_AUTO_TRAIN must be 0 or 1")
        auto_train = configured == "1"
    core = CoreTraining(directory / "core", enabled=auto_train)
    resources = {}
    capture_resources = {}
    resource_lock = BoundedSemaphore(1)
    compute_slots = BoundedSemaphore(2)

    def get_resources():
        with resource_lock:
            if not resources:
                resources["store"] = SessionStore(directory / "sessions.sqlite3")
                resources["manager"] = ResearchManager(directory / "models")
        return resources["store"], resources["manager"]

    def get_network_store():
        with resource_lock:
            if "store" not in capture_resources:
                capture_resources["store"] = NetworkStore(directory / "capture.sqlite3")
        return capture_resources["store"]

    def get_capture_store():
        with resource_lock:
            if "business" not in capture_resources:
                capture_resources["business"] = NetworkStore(
                    directory / "capture.sqlite3",
                    stream="capture",
                    policy_path=directory / "core" / "training.sqlite3",
                )
        return capture_resources["business"]

    @asynccontextmanager
    async def lifespan(app):
        core.start()
        learning_stop = Event()
        learning_done = asyncio.Event()

        async def learn():
            while not learning_stop.is_set():
                try:
                    if (directory / "capture.sqlite3").is_file():
                        store = await asyncio.to_thread(get_capture_store)
                        await asyncio.to_thread(store.train_behavior, learning_stop.is_set)
                        await asyncio.to_thread(
                            store.collect_training_openings, learning_stop.is_set
                        )
                    capture_resources.pop("learning_error", None)
                except CancellationError:
                    break
                except (OSError, ValueError, sqlite3.Error) as error:
                    capture_resources["learning_error"] = type(error).__name__
                    logging.getLogger(__name__).exception(
                        "Capture learners failed; active models retained"
                    )
                try:
                    await asyncio.wait_for(learning_done.wait(), timeout=30)
                except TimeoutError:
                    pass

        learner = asyncio.create_task(learn())
        supervisor = asyncio.create_task(
            core.supervise(
                lambda: "manager" in resources and resources["manager"].has_active_jobs()
            )
        )
        try:
            yield
        finally:
            learning_stop.set()
            learning_done.set()
            await learner
            await asyncio.to_thread(core.close)
            await supervisor
            if resources:
                resources["manager"].close()

    app = FastAPI(
        title="Texas GTO Pre1.0",
        version="0.8.3",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.core_training = core
    app.add_middleware(
        LocalBoundary, origins=allowed_origins, token=token, foreground=core.foreground
    )
    if token is None:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_origin_regex=EXTENSION_ORIGIN if token is None else None,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Texas-Local"],
        allow_credentials=False,
    )

    @app.exception_handler(ConflictError)
    async def conflict(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(QueueFullError)
    async def queue_full(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.websocket("/v1/research/stream")
    async def capture_stream(ws: WebSocket):
        await network_stream(
            ws,
            origins=allowed_origins,
            token=token,
            get_store=get_network_store,
            get_capture_store=get_capture_store,
            foreground=core.foreground,
        )

    @app.get("/v1/health", response_model=HealthResponse)
    def health():
        return HealthResponse()

    @app.get("/v1/core-training", response_model=CoreTrainingResponse)
    def core_training():
        return core.status()

    @app.get("/v1/capture/learning")
    def learning(request: Request):
        origin = request.headers.get("origin")
        if origin is None:
            raise HTTPException(400, "An Origin is required for capture learning")
        return {
            **get_capture_store().learning_status(origin),
            "error": capture_resources.get("learning_error"),
        }

    @app.post("/v1/capture/learning/rollback")
    def rollback_learning(request: Request, body: RollbackBehavior):
        origin = request.headers.get("origin")
        if origin is None:
            raise HTTPException(400, "An Origin is required for capture learning")
        return {
            "active_version": get_capture_store().rollback_behavior(origin, body.expected_version)
        }

    @app.post("/v1/sessions", response_model=SessionResponse)
    def create_session(request: CreateSession):
        return get_resources()[0].create(request)

    @app.get("/v1/sessions/{session_id}", response_model=SessionResponse)
    def session(session_id: UUID, viewer: int | None = Query(default=None, ge=0, le=7)):
        return get_resources()[0].snapshot(str(session_id), viewer)

    @app.post("/v1/sessions/{session_id}/actions", response_model=SessionResponse)
    def action(session_id: UUID, request: SubmitAction):
        return get_resources()[0].act(str(session_id), request)

    @app.get("/v1/sessions/{session_id}/profiles")
    def profiles(session_id: UUID, as_of: int | None = Query(default=None, ge=0)):
        return get_resources()[0].public_profiles(str(session_id), as_of)

    @app.delete("/v1/sessions/{session_id}")
    def delete_session(session_id: UUID):
        get_resources()[0].delete(str(session_id))
        return {"deleted": True}

    @app.post("/v1/training", response_model=JobResponse)
    def train(request: TrainRequest):
        return get_resources()[1].submit(request)

    @app.get("/v1/training/{job_id}", response_model=JobResponse)
    def job(job_id: UUID):
        return get_resources()[1].job(str(job_id))

    @app.delete("/v1/training/{job_id}", response_model=JobResponse)
    def cancel(job_id: UUID):
        return get_resources()[1].cancel(str(job_id))

    @app.get("/v1/blueprints")
    def blueprints():
        return get_resources()[1].blueprints()

    @app.post("/v1/analyze", response_model=AnalysisResponse)
    def analysis(request: AnalyzeRequest):
        if not compute_slots.acquire(blocking=False):
            raise HTTPException(429, "Research compute slots are busy")
        try:
            store, manager = get_resources()
            return analyze(store, manager, request)
        finally:
            compute_slots.release()

    @app.post("/v1/equity")
    def equity(request: EquityRequest):
        if not compute_slots.acquire(blocking=False):
            raise HTTPException(429, "Research compute slots are busy")
        try:
            return asdict(
                estimate_equity(
                    request.hero,
                    request.board,
                    opponents=request.opponents,
                    ranges=request.ranges,
                    samples=request.samples,
                    seed=request.seed,
                )
            )
        finally:
            compute_slots.release()

    @app.post("/v1/hand-strength")
    def hand_strength(request: HandStrengthRequest):
        if not compute_slots.acquire(blocking=False):
            raise HTTPException(429, "Research compute slots are busy")
        try:
            return analyze_hand_strength(
                request.hero,
                request.board,
                opponent_range=request.opponent_range,
                max_beating_combos=request.max_beating_combos,
            ).to_dict()
        finally:
            compute_slots.release()

    @app.post("/v1/experiments/adaptation")
    def adaptation(request: AdaptationRequest):
        if not compute_slots.acquire(blocking=False):
            raise HTTPException(429, "Research compute slots are busy")
        try:
            manager = get_resources()[1]
            game = manager.game(request.blueprint_id)
            blueprint = manager.blueprint(request.blueprint_id, game)
            model = ContinuationPolicy(blueprint.policy, request.opponent_mode)
            result = adapt_strategy(
                game,
                blueprint.policy,
                model,
                hero=request.hero,
                sample_count=request.sample_count,
                max_worst_case_loss=request.max_worst_case_loss,
                max_nodes=200_000,
                deadline_seconds=5.0,
            )
            return {
                "schema_version": "1.1",
                "blueprint_id": request.blueprint_id,
                "hero": request.hero,
                "opponent_mode": request.opponent_mode,
                "model_source": "synthetic_bias",
                "result": result.to_dict(),
            }
        finally:
            compute_slots.release()

    return app
