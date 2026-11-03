"""Public opening curricula from causal capture prefixes, without copying private cards."""

import hashlib
import json
from collections import Counter
from functools import partial

from texas_strategy.mushroom_population import public_opening

from .capture_contract import BusinessEvent
from .capture_ledger import derive_recorded_mushroom_ledger
from .capture_recommendation import prepare_query
from .capture_state import reduce_event

MAX_OPENINGS = 512
MAX_BATCH = 64
MAX_HAND_EVENTS = 4096


def initialize(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS capture_training_openings (id INTEGER PRIMARY KEY, "
        "origin TEXT, source TEXT, hand TEXT, through TEXT, root_hash TEXT, root TEXT, "
        "UNIQUE(origin,source,hand))"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS capture_training_openings_hash ON "
        "capture_training_openings(root_hash)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS capture_training_opening_progress "
        "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), through_row INTEGER, "
        "rejected TEXT, limited INTEGER)"
    )
    db.execute("INSERT OR IGNORE INTO capture_training_opening_progress VALUES(1,0,'{}',0)")


def _prefix(db, origin, source, through, hand):
    # Include earlier gaps in this hand, even if a later hand_started repeats its identifier.
    start = db.execute(
        "SELECT sequence FROM capture_events WHERE origin=? AND source=? AND sequence<=? "
        "AND json_extract(body,'$.hand_id')=? AND json_extract(body,'$.kind') "
        "NOT IN ('settlement','settlement_pending') ORDER BY sequence LIMIT 1",
        (origin, source, through, hand),
    ).fetchone()
    if start is None:
        return []
    rows = db.execute(
        "SELECT body FROM capture_events WHERE origin=? AND source=? AND sequence>=? "
        "AND sequence<=? ORDER BY sequence LIMIT ?",
        (origin, source, start[0], through, MAX_HAND_EVENTS + 1),
    ).fetchall()
    if len(rows) > MAX_HAND_EVENTS:
        raise ValueError("hand_event_budget")
    return [json.loads(row[0]) for row in rows]


def collect(db, check=lambda: None):
    """Read a bounded snapshot; the caller publishes after releasing this read transaction."""
    cursor = db.execute("SELECT through_row FROM capture_training_opening_progress").fetchone()[0]
    head = db.execute("SELECT COALESCE(MAX(rowid),0) FROM capture_events").fetchone()[0]
    if type(cursor) is not int or not 0 <= cursor <= head:
        raise ValueError("Opening learner cursor is outside the capture history")
    rows = db.execute(
        "SELECT rowid,origin,source,sequence,body FROM capture_events WHERE rowid>? "
        "AND rowid<=? AND json_extract(body,'$.kind')='snapshot' "
        "AND json_extract(body,'$.data.turn.player_id')='hero' ORDER BY rowid LIMIT ?",
        (cursor, head, MAX_BATCH),
    ).fetchall()
    candidates, rejected = [], Counter()
    for _, origin, source, through, body in rows:
        check()
        event = BusinessEvent.model_validate_json(body)
        try:
            prefix = _prefix(db, origin, source, through, event.hand_id)
            state = None
            for item in prefix:
                check()
                state = reduce_event(state, BusinessEvent.model_validate(item))
            if state is None:
                raise ValueError("missing_hand_start")
            model, _, _ = prepare_query(
                state,
                prefix,
                ledger_resolver=partial(
                    derive_recorded_mushroom_ledger,
                    db,
                    origin=origin,
                    source=source,
                    through=through,
                    check=check,
                ),
            )
            root = json.dumps(public_opening(model), sort_keys=True, separators=(",", ":"))
        except ValueError as error:
            rejected[str(error)[:80]] += 1
            continue
        candidates.append(
            (
                origin,
                source,
                event.hand_id,
                through,
                hashlib.sha256(root.encode()).hexdigest(),
                root,
            )
        )
    return {
        "previous": cursor,
        "through": rows[-1][0] if len(rows) == MAX_BATCH else head,
        "candidates": candidates,
        "rejected": rejected,
    }


def publish(db, batch):
    cursor, raw_rejected, limited = db.execute(
        "SELECT through_row,rejected,limited FROM capture_training_opening_progress"
    ).fetchone()
    if cursor != batch["previous"]:
        raise ValueError("Opening learner advanced concurrently")
    known = {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT root_hash FROM capture_training_openings LIMIT ?", (MAX_OPENINGS + 1,)
        )
    }
    if len(known) > MAX_OPENINGS:
        raise ValueError("Captured opening registry exceeds capacity")
    rejected = Counter(json.loads(raw_rejected)) + batch["rejected"]
    for candidate in batch["candidates"]:
        old = db.execute(
            "SELECT root_hash FROM capture_training_openings "
            "WHERE origin=? AND source=? AND hand=?",
            candidate[:3],
        ).fetchone()
        if old is not None:
            if old[0] != candidate[4]:
                rejected["conflicting_public_opening"] += 1
            continue
        if candidate[4] not in known and len(known) >= MAX_OPENINGS:
            limited = 1
            rejected["opening_capacity"] += 1
            continue
        db.execute(
            "INSERT INTO capture_training_openings(origin,source,hand,through,root_hash,root) "
            "VALUES(?,?,?,?,?,?)",
            candidate,
        )
        known.add(candidate[4])
    db.execute(
        "UPDATE capture_training_opening_progress SET through_row=?,rejected=?,limited=? "
        "WHERE singleton=1",
        (batch["through"], json.dumps(dict(rejected), sort_keys=True), limited),
    )


def load(db):
    """Only public geometry crosses into the one shared training policy."""
    roots = []
    for digest, raw in db.execute(
        "SELECT root_hash,root FROM capture_training_openings GROUP BY root_hash,root "
        "ORDER BY MIN(id) LIMIT ?",
        (MAX_OPENINGS + 1,),
    ):
        if (
            not isinstance(raw, str)
            or not isinstance(digest, str)
            or len(raw.encode()) > 4096
            or hashlib.sha256(raw.encode()).hexdigest() != digest
        ):
            raise ValueError("Invalid captured opening digest")
        roots.append(json.loads(raw))
    if len(roots) > MAX_OPENINGS:
        raise ValueError("Captured opening registry exceeds capacity")
    progress = db.execute("SELECT limited FROM capture_training_opening_progress").fetchone()
    if progress is None or type(progress[0]) is not int or progress[0] not in (0, 1):
        raise ValueError("Invalid opening learner capacity marker")
    return roots, bool(progress[0])
