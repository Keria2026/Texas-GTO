"""Durable TGTO network/business receipts and observed (never simulated) hand state."""

import asyncio
import hmac
import json
import re
import sqlite3
from collections import OrderedDict
from contextlib import closing, nullcontext
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from texas_strategy.evaluation import CancellationError
from texas_strategy.policy import TabularPolicy

from . import capture_learning, capture_openings, capture_strategy
from .capture_contract import (
    CAPABILITY as BUSINESS_CAPABILITY,
)
from .capture_contract import (
    SCHEMA as BUSINESS_SCHEMA,
)
from .capture_contract import (
    STATE_CAPABILITY,
    BusinessBatch,
    BusinessEvent,
)
from .capture_ledger import derive_mushroom_ledger, derive_recorded_mushroom_ledger
from .capture_recommendation import CAPABILITY as RECOMMENDATIONS_CAPABILITY
from .capture_recommendation import PR_CAPABILITY, SEARCH_CAPABILITY, recommend
from .capture_replay import ReplayUnavailable, replay_capture
from .capture_state import reduce_event
from .capture_state import response as capture_response

SCHEMA = "tgto.network/0.1"
STATUS_SCHEMA = "tgto.network-status/0.1"
PROFILES_CAPABILITY = "capture_profiles_v1"
CAPABILITY = "network_events_v1"
STATUS_CAPABILITY = "network_status_v1"
MAX_MESSAGE = 256 * 1024
MAX_REPLAY_EVENTS = 4096
MAX_U64 = (1 << 64) - 1
Source = Annotated[str, Field(pattern=r"^[a-zA-Z0-9-]{1,80}$")]
Short = Annotated[str, Field(max_length=512)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class NetworkData(StrictModel):
    connection_id: Short | None = None
    endpoint: Short | None = None
    direction: Literal["sent", "received"] | None = None
    opcode: Literal[1, 2, 8, 9, 10] | None = None
    byte_length: Annotated[int, Field(ge=0, le=1024 * 1024)] | None = None
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    monotonic_time: Annotated[float, Field(ge=0)] | None = None
    reason: Short | None = None

    @model_validator(mode="before")
    @classmethod
    def no_explicit_null_or_boolean(cls, data):
        if not isinstance(data, dict) or any(
            v is None or isinstance(v, bool) for v in data.values()
        ):
            raise ValueError("Metadata values must be present scalars")
        return data

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value):
        if value is not None:
            url = urlsplit(value)
            if (
                url.scheme not in ("ws", "wss")
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or "?" in value
                or "#" in value
                or any(c.isspace() or ord(c) < 32 for c in value)
            ):
                raise ValueError("Endpoint must not contain credentials, query or fragment")
            _ = url.port
        return value


class NetworkEvent(StrictModel):
    wire_schema: Literal["tgto.network/0.1"] = Field(alias="schema")
    type: Literal["network_message", "capture_gap", "socket_open", "socket_closed", "socket_error"]
    source_id: Source
    sequence: str
    captured_at: Annotated[str, Field(max_length=64)]
    data: NetworkData

    @field_validator("sequence")
    @classmethod
    def valid_sequence(cls, value):
        if not re.fullmatch(r"[1-9][0-9]{0,19}", value) or int(value) > MAX_U64:
            raise ValueError("Sequence must be a positive u64 decimal string")
        return value

    @field_validator("captured_at")
    @classmethod
    def timestamp(cls, value):
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("A timezone is required")
        return value

    @model_validator(mode="after")
    def valid_frame(self):
        d = self.data
        if self.type == "network_message":
            if any(v is None for v in (d.direction, d.opcode, d.byte_length, d.sha256)):
                raise ValueError("Incomplete frame metadata")
            if d.opcode >= 8 and d.byte_length > 125:
                raise ValueError("Oversized control frame")
        return self


class NetworkBatch(StrictModel):
    wire_schema: Literal["tgto.network/0.1"] = Field(alias="schema")
    type: Literal["network.append"]
    source_id: Source
    events: Annotated[list[NetworkEvent], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def contiguous(self):
        previous = None
        for event in self.events:
            if event.source_id != self.source_id:
                raise ValueError("Source mismatch")
            if previous is not None and int(event.sequence) != previous + 1:
                raise ValueError("Noncontiguous batch")
            previous = int(event.sequence)
        return self


class StreamAuth(StrictModel):
    type: Literal["auth"]
    token: Annotated[str, Field(max_length=4096)] = ""
    capabilities: Annotated[
        list[
            Literal[
                "network_status_v1",
                "capture_state_v1",
                "capture_profiles_v1",
                "capture_recommendations_v1",
                "capture_search_v1",
                "capture_pr_v1",
            ]
        ],
        Field(max_length=6),
    ] = []


class NetworkStore:
    """Atomic replay-safe receipts scoped to the configured extension Origin.

    One deployment token is a single-user boundary, not multi-tenant authentication.
    ACKs cover only the submitted batch, never a later persisted head.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes=256 * 1024 * 1024,
        max_sources=256,
        stream="network",
        policy_path=None,
    ):
        if stream not in ("network", "capture"):
            raise ValueError("Unknown stream")
        self.stream = stream
        self.search_memory = OrderedDict()
        self.search_lock = Lock()
        self.learning_lock = Lock()
        self.policy_path = policy_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        self.path, self.max_bytes, self.max_sources = path, max_bytes, max_sources
        with closing(self.connect()) as db:
            db.executescript(f"""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS {self.stream}_events (
                    origin TEXT NOT NULL, source TEXT NOT NULL, sequence TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(origin,source,sequence));
                CREATE TABLE IF NOT EXISTS {self.stream}_heads (
                    origin TEXT NOT NULL, source TEXT NOT NULL, sequence TEXT NOT NULL,
                    bytes INTEGER NOT NULL, PRIMARY KEY(origin,source));
            """)
            if stream == "capture":
                db.execute(
                    "CREATE TABLE IF NOT EXISTS capture_state (origin TEXT NOT NULL, "
                    "source TEXT NOT NULL, sequence TEXT NOT NULL, body TEXT NOT NULL, "
                    "PRIMARY KEY(origin,source))"
                )
                with db:
                    db.execute("BEGIN IMMEDIATE")
                    capture_learning.initialize(db)
                    self._restore_learning(db)
                    capture_strategy.initialize(db)
                    capture_openings.initialize(db)

    def collect_training_openings(self, should_cancel=lambda: False):
        def check():
            if should_cancel():
                raise CancellationError("Opening collection cancelled")

        with self.learning_lock:
            with closing(self.connect()) as db, db:
                db.execute("BEGIN")
                batch = capture_openings.collect(db, check)
            check()
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                capture_openings.publish(db, batch)

    def train_behavior(self, should_cancel=lambda: False):
        if not self.learning_lock.acquire(blocking=False):
            return

        def check():
            if should_cancel():
                raise CancellationError("Behavior training cancelled")

        try:
            with closing(self.connect()) as db:
                origins = [
                    row[0] for row in db.execute("SELECT DISTINCT origin FROM capture_heads")
                ]
            for origin in origins:
                check()
                with closing(self.connect()) as db, db:
                    db.execute("BEGIN")
                    active = capture_strategy.load(db, origin)
                    boundary = db.execute(
                        "SELECT COALESCE(MAX(e.rowid),0) FROM capture_learning_outcomes o "
                        "JOIN capture_events e ON e.origin=o.origin AND e.source=o.source "
                        "AND e.sequence=o.sequence WHERE o.origin=? AND "
                        "json_extract(o.body,'$.eligible_for_completed_hand_dataset')=1",
                        (origin,),
                    ).fetchone()[0]
                    if db.execute(
                        "SELECT 1 FROM capture_strategy_runs WHERE origin=? AND boundary=?",
                        (origin, boundary),
                    ).fetchone():
                        continue
                    samples, excluded, boundary = capture_strategy.dataset(db, origin, check)
                    report, candidate = capture_strategy.candidate(
                        samples, boundary=boundary, excluded=excluded, active=active
                    )
                check()
                with closing(self.connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    capture_strategy.publish(
                        db,
                        origin,
                        report,
                        candidate,
                        expected=active["version"] if active else None,
                    )
        finally:
            self.learning_lock.release()

    def learning_status(self, origin):
        with closing(self.connect()) as db:
            return capture_strategy.status(db, origin)

    def rollback_behavior(self, origin, expected):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            return capture_strategy.rollback(db, origin, expected)

    def _restore_learning(self, db):
        for origin, source, head in db.execute("SELECT origin,source,sequence FROM capture_heads"):
            learned = db.execute(
                "SELECT sequence FROM capture_learning_heads WHERE origin=? AND source=?",
                (origin, source),
            ).fetchone()
            if learned and int(learned[0]) == int(head):
                continue
            projection = None
            for (body,) in db.execute(
                "SELECT body FROM capture_events WHERE origin=? AND source=? ORDER BY sequence",
                (origin, source),
            ):
                event = BusinessEvent.model_validate_json(body)
                before = projection
                projection = reduce_event(projection, event)
                capture_learning.observe(db, origin, event, before, projection)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def append(self, origin: str, batch: NetworkBatch | BusinessBatch) -> str:
        with closing(self.connect()) as db, db:
            if (self.stream == "capture") != isinstance(batch, BusinessBatch):
                raise ValueError("Store stream mismatch")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT sequence,bytes FROM {self.stream}_heads WHERE origin=? AND source=?",
                (origin, batch.source_id),
            ).fetchone()
            last, size = (int(row[0]), row[1]) if row else (0, 0)
            total, sources = db.execute(
                f"SELECT COALESCE(SUM(bytes),0),COUNT(*) FROM {self.stream}_heads"
            ).fetchone()
            if row is None and sources >= self.max_sources:
                raise ValueError("Capture source quota exceeded")
            projection = None
            if self.stream == "capture":
                stored = db.execute(
                    "SELECT body FROM capture_state WHERE origin=? AND source=?",
                    (origin, batch.source_id),
                ).fetchone()
                projection = json.loads(stored[0]) if stored else None
            for event in batch.events:
                body = json.dumps(
                    event.model_dump(exclude_unset=True, by_alias=True),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
                key = (origin, batch.source_id, event.sequence.zfill(20))
                old = db.execute(
                    f"SELECT body FROM {self.stream}_events "
                    "WHERE origin=? AND source=? AND sequence=?",
                    key,
                ).fetchone()
                if old:
                    if old[0] != body:
                        raise ValueError("Conflicting sequence")
                    continue
                if int(event.sequence) != last + 1:
                    raise ValueError("Sequence gap")
                cost = len(body.encode("utf-8"))
                size += cost
                total += cost
                if size > 64 * 1024 * 1024 or total > self.max_bytes:
                    raise ValueError("Capture storage quota exceeded")
                db.execute(f"INSERT INTO {self.stream}_events VALUES(?,?,?,?)", (*key, body))
                if self.stream == "capture":
                    before = projection
                    projection = reduce_event(projection, event)
                    capture_learning.observe(db, origin, event, before, projection)
                last += 1
            db.execute(
                f"INSERT INTO {self.stream}_heads VALUES(?,?,?,?) ON CONFLICT(origin,source) "
                "DO UPDATE SET sequence=excluded.sequence,bytes=excluded.bytes",
                (origin, batch.source_id, str(last), size),
            )
            if self.stream == "capture":
                db.execute(
                    "INSERT INTO capture_state VALUES(?,?,?,?) ON CONFLICT(origin,source) "
                    "DO UPDATE SET sequence=excluded.sequence,body=excluded.body",
                    (
                        origin,
                        batch.source_id,
                        str(last),
                        json.dumps(projection, separators=(",", ":")),
                    ),
                )
        return batch.events[-1].sequence

    def state(self, origin: str, source: str):
        observed = self._observation(origin, source)
        if observed is None:
            return None
        through, state, prefix = observed
        return self._state_response(
            source,
            through,
            state,
            prefix,
            ledger_resolver=lambda **kwargs: self._ledger(origin, source, through, **kwargs),
        )

    def _ledger(self, origin, source, through, *, hand_id, fees, should_cancel=None):
        def check():
            if should_cancel is not None and should_cancel():
                raise CancellationError("Observation changed; no ledger available")

        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            return derive_recorded_mushroom_ledger(
                db,
                origin=origin,
                source=source,
                through=through,
                hand_id=hand_id,
                fees=fees,
                check=check,
            )

    def _observation(self, origin, source):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT sequence,body FROM capture_state WHERE origin=? AND source=?",
                (origin, source),
            ).fetchone()
            if row is None:
                return None
            state = json.loads(row[1])
            prefix = self._public_hand_prefix(db, origin, source, row[0], state)
        return row[0], state, prefix

    def recommendation(self, origin, source, should_cancel=None, *, search=False, pr=False):
        observed = self._observation(origin, source)
        if observed is None:
            return None
        through, state, prefix = observed
        context, opponent = None, None
        memory_key = origin, source, state["hand_id"]
        if pr:
            with self.search_lock:
                context = {"previous": self.search_memory.get(memory_key, ())}
            with closing(self.connect()) as db, db:
                db.execute("BEGIN")
                try:
                    opponent = capture_strategy.load(db, origin)
                except ValueError:
                    opponent = None
                event_id = db.execute(
                    "SELECT rowid FROM capture_events WHERE origin=? AND source=? AND sequence=?",
                    (origin, source, through.zfill(20)),
                ).fetchone()
                if opponent and (event_id is None or event_id[0] <= opponent["available_after"]):
                    opponent = None
        result = recommend(
            source=source,
            through=through,
            state=state,
            prefix=prefix,
            policy_path=self.policy_path,
            should_cancel=should_cancel,
            search=search,
            **({"pr_context": context, "opponent_model": opponent} if pr else {}),
            ledger_resolver=lambda **kwargs: self._ledger(
                origin, source, through, should_cancel=should_cancel, **kwargs
            ),
        )
        # Another connection may commit while the pinned model is being queried.
        with closing(self.connect()) as db:
            current = db.execute(
                "SELECT sequence FROM capture_state WHERE origin=? AND source=?", (origin, source)
            ).fetchone()
        if current is None or current[0] != through:
            return None
        if should_cancel is not None and should_cancel():
            return None
        if result["status"] == "ready" and context is not None and "record" in context:
            record = context["record"]
            with self.search_lock:
                previous = self.search_memory.pop(memory_key, ())
                published = {}
                for old in previous:
                    if old["street"] == record["street"]:
                        published.update(old["published"].entries)
                        if not record["average"].entries:
                            record["current"], record["average"] = old["current"], old["average"]
                published.update(record["published"].entries)
                record["published"] = TabularPolicy(published, missing="error")
                self.search_memory[memory_key] = tuple(
                    r for r in previous if r["street"] != record["street"]
                ) + (record,)
                while len(self.search_memory) > 2:
                    self.search_memory.popitem(last=False)
        return result

    @staticmethod
    def _state_response(source, through, state, prefix, *, ledger_resolver=None):
        verified = ledger_verified = False
        if prefix:
            try:
                replay = replay_capture(prefix)
                verified = True
            except ReplayUnavailable:
                pass
            else:
                try:
                    if ledger_resolver is not None:
                        ledger_resolver(hand_id=state["hand_id"], fees=replay.fees)
                    else:
                        derive_mushroom_ledger(
                            hand_id=state["hand_id"],
                            fees=replay.fees,
                            settlements=state["settlements"],
                        )
                    ledger_verified = True
                except ValueError:
                    pass
        result = capture_response(
            source,
            through,
            state,
            public_hand_verified=verified,
            ledger_verified=ledger_verified,
        )
        if prefix and not verified:
            result["analysis"]["missing_fields"].append("public_betting_replay")
        return result

    @staticmethod
    def _public_hand_prefix(db, origin, source, through, state):
        if (
            not state["history_complete"]
            or state["gap"]
            or state["closed"]
            or state["street"] not in ("preflop", "flop", "turn", "river")
            or not 4 <= len(state["players"]) <= 8
        ):
            return []
        head = through.zfill(20)
        start = db.execute(
            "SELECT sequence FROM capture_events WHERE origin=? AND source=? AND sequence<=? "
            "AND json_extract(body,'$.kind')='hand_started' "
            "AND json_extract(body,'$.hand_id')=? ORDER BY sequence DESC LIMIT 1",
            (origin, source, head, state["hand_id"]),
        ).fetchone()
        if start is None:
            return []
        rows = db.execute(
            "SELECT body FROM capture_events WHERE origin=? AND source=? "
            "AND sequence>=? AND sequence<=? ORDER BY sequence LIMIT ?",
            (origin, source, start[0], head, MAX_REPLAY_EVENTS + 1),
        ).fetchall()
        # Never truncate a long hand and call its remaining history complete.
        if len(rows) > MAX_REPLAY_EVENTS:
            return []
        return [json.loads(body) for (body,) in rows]

    def profiles(self, origin: str, source: str):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT sequence,body FROM capture_state WHERE origin=? AND source=?",
                (origin, source),
            ).fetchone()
            if row is None:
                return None
            state = json.loads(row[1])
            result = capture_learning.profiles(
                db, origin, source, player_ids=[p["player_id"] for p in state["players"]]
            )
            if result["through_sequence"] != row[0]:
                raise ValueError("Learning state is behind captured state")
            return {"schema": "tgto.capture-profiles/0.1", "type": "capture.profiles", **result}


def blocked_status(source: str, through: str) -> dict:
    return {
        "type": "network.status",
        "schema": STATUS_SCHEMA,
        "source_id": source,
        "through_sequence": through,
        "status": "blocked",
        "reason": "business_events_unavailable",
        "actions": [],
        "missing_fields": ["rules", "hand_id", "hero_cards", "board", "players", "action_history"],
    }


async def receive_json(ws: WebSocket, timeout: float) -> dict:
    frame = await asyncio.wait_for(ws.receive(), timeout)
    if frame["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(frame.get("code", 1000))
    text = frame.get("text")
    if text is None or len(text.encode("utf-8")) > MAX_MESSAGE:
        raise ValueError("Text JSON frame exceeds protocol boundary")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Object required")
    return value


async def network_stream(
    ws: WebSocket, *, origins, token: str | None, get_store, get_capture_store, foreground=None
):
    origin = ws.headers.get("origin", "")
    if (token is not None and origin not in origins) or not re.fullmatch(
        r"chrome-extension://[a-p]{32}", origin
    ):
        await ws.close(code=4403)
        return
    await ws.accept()
    incoming = calculation = cancelled = pending = None
    try:
        auth = StreamAuth.model_validate(await receive_json(ws, 5))
        if token is not None and not hmac.compare_digest(auth.token.encode(), token.encode()):
            await ws.close(code=4401)
            return
        await ws.send_json(
            {
                "type": "ready",
                "schema_version": "1.2",
                "capabilities": [
                    CAPABILITY,
                    STATUS_CAPABILITY,
                    BUSINESS_CAPABILITY,
                    STATE_CAPABILITY,
                    PROFILES_CAPABILITY,
                    RECOMMENDATIONS_CAPABILITY,
                    SEARCH_CAPABILITY,
                    PR_CAPABILITY,
                ],
            }
        )
        store = await run_in_threadpool(get_store)
        capture_store = await run_in_threadpool(get_capture_store)

        async def calculate(source, stop):
            with foreground() if foreground is not None else nullcontext():
                try:
                    return await run_in_threadpool(
                        capture_store.recommendation,
                        origin,
                        source,
                        stop.is_set,
                        **({"search": True} if SEARCH_CAPABILITY in auth.capabilities else {}),
                        **({"pr": True} if PR_CAPABILITY in auth.capabilities else {}),
                    )
                except CancellationError:
                    return None

        incoming = asyncio.create_task(receive_json(ws, 60))
        while True:
            if calculation is None and pending is not None:
                requested_source, requested_through = pending
                pending, cancelled = None, Event()
                calculation = asyncio.create_task(calculate(requested_source, cancelled))
            done, _ = await asyncio.wait(
                [incoming] + ([calculation] if calculation is not None else []),
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Incoming changes win over a computation that finished at the same time.
            if incoming in done:
                message = incoming.result()
                incoming = asyncio.create_task(receive_json(ws, 60))
                if message == {"type": "ping"}:
                    await ws.send_json({"type": "pong"})
                elif message.get("type") == "capture.append":
                    batch = BusinessBatch.model_validate(message)
                    if cancelled is not None:
                        cancelled.set()
                    pending = None
                    with foreground() if foreground is not None else nullcontext():
                        through = await run_in_threadpool(capture_store.append, origin, batch)
                        await ws.send_json(
                            {
                                "schema": BUSINESS_SCHEMA,
                                "type": "capture.ack",
                                "source_id": batch.source_id,
                                "through_sequence": through,
                            }
                        )
                        if STATE_CAPABILITY in auth.capabilities:
                            state = await run_in_threadpool(
                                capture_store.state, origin, batch.source_id
                            )
                            if state and state["through_sequence"] == through:
                                await ws.send_json(state)
                        if PROFILES_CAPABILITY in auth.capabilities:
                            profiles = await run_in_threadpool(
                                capture_store.profiles, origin, batch.source_id
                            )
                            if profiles and profiles["through_sequence"] == through:
                                await ws.send_json(profiles)
                        if RECOMMENDATIONS_CAPABILITY in auth.capabilities:
                            pending = batch.source_id, through
                else:
                    batch = NetworkBatch.model_validate(message)
                    through = await run_in_threadpool(store.append, origin, batch)
                    await ws.send_json(
                        {
                            "schema": SCHEMA,
                            "type": "network.ack",
                            "source_id": batch.source_id,
                            "through_sequence": through,
                        }
                    )
                    if STATUS_CAPABILITY in auth.capabilities:
                        await ws.send_json(blocked_status(batch.source_id, through))
            if calculation in done:
                result = calculation.result()
                calculation = None
                if (
                    not cancelled.is_set()
                    and result
                    and (result["source_id"], result["through_sequence"])
                    == (requested_source, requested_through)
                ):
                    await ws.send_json(result)
    except WebSocketDisconnect:
        pass
    except TimeoutError:
        await ws.close(code=1008, reason="Stream timeout")
    except (ValueError, ValidationError, RecursionError):
        await ws.send_json({"type": "error", "code": "invalid_or_conflicting_request"})
        await ws.close(code=1008, reason="Request rejected")
    except (sqlite3.Error, OSError):
        # No ACK on a failed commit; the client retains and retries its outbox.
        await ws.close(code=1011, reason="Storage unavailable")
    finally:
        if cancelled is not None:
            cancelled.set()
        if incoming is not None:
            incoming.cancel()
        # A cancelled await does not stop a worker thread; keep it owned until it exits.
        await asyncio.gather(
            *(task for task in (incoming, calculation) if task is not None),
            return_exceptions=True,
        )
