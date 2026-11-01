"""TGTO network metadata ingestion. This stream never constructs a poker hand."""

import asyncio
import hmac
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

SCHEMA = "tgto.network/0.1"
STATUS_SCHEMA = "tgto.network-status/0.1"
CAPABILITY = "network_events_v1"
STATUS_CAPABILITY = "network_status_v1"
MAX_MESSAGE = 256 * 1024
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
    token: Annotated[str, Field(min_length=1, max_length=4096)]
    capabilities: Annotated[list[Literal["network_status_v1"]], Field(max_length=1)] = []


class NetworkStore:
    """Atomic replay-safe receipts scoped to the configured extension Origin.

    One deployment token is a single-user boundary, not multi-tenant authentication.
    ACKs cover only the submitted batch, never a later persisted head.
    """

    def __init__(self, path: Path, *, max_bytes=256 * 1024 * 1024, max_sources=256):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        self.path, self.max_bytes, self.max_sources = path, max_bytes, max_sources
        with closing(self.connect()) as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS network_events (
                    origin TEXT NOT NULL, source TEXT NOT NULL, sequence TEXT NOT NULL,
                    body TEXT NOT NULL, PRIMARY KEY(origin,source,sequence));
                CREATE TABLE IF NOT EXISTS network_heads (
                    origin TEXT NOT NULL, source TEXT NOT NULL, sequence TEXT NOT NULL,
                    bytes INTEGER NOT NULL, PRIMARY KEY(origin,source));
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def append(self, origin: str, batch: NetworkBatch) -> str:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sequence,bytes FROM network_heads WHERE origin=? AND source=?",
                (origin, batch.source_id),
            ).fetchone()
            last, size = (int(row[0]), row[1]) if row else (0, 0)
            total, sources = db.execute(
                "SELECT COALESCE(SUM(bytes),0),COUNT(*) FROM network_heads"
            ).fetchone()
            if row is None and sources >= self.max_sources:
                raise ValueError("Capture source quota exceeded")
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
                    "SELECT body FROM network_events WHERE origin=? AND source=? AND sequence=?",
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
                db.execute("INSERT INTO network_events VALUES(?,?,?,?)", (*key, body))
                last += 1
            db.execute(
                "INSERT INTO network_heads VALUES(?,?,?,?) ON CONFLICT(origin,source) "
                "DO UPDATE SET sequence=excluded.sequence,bytes=excluded.bytes",
                (origin, batch.source_id, str(last), size),
            )
        return batch.events[-1].sequence


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


async def network_stream(ws: WebSocket, *, origins, token: str, get_store):
    origin = ws.headers.get("origin", "")
    if origin not in origins or not re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
        await ws.close(code=4403)
        return
    await ws.accept()
    try:
        auth = StreamAuth.model_validate(await receive_json(ws, 5))
        if not hmac.compare_digest(auth.token.encode(), token.encode()):
            await ws.close(code=4401)
            return
        await ws.send_json(
            {
                "type": "ready",
                "schema_version": "1.2",
                "capabilities": [CAPABILITY, STATUS_CAPABILITY],
            }
        )
        store = await run_in_threadpool(get_store)
        while True:
            message = await receive_json(ws, 60)
            if message == {"type": "ping"}:
                await ws.send_json({"type": "pong"})
                continue
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
