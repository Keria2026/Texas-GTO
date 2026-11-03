"""Transactional, source-scoped learning from observed actions, never action EV.

The caller validates and durably appends events in this same SQLite transaction.
Decision context is frozen at an observed turn/snapshot boundary; confirmation
can arrive after its cash debit. Later enrichment has its own availability sequence.
No hidden opponent cards or future board enter it.
"""

import json
from collections import defaultdict

from .capture_contract import U64, BusinessEvent, alias

ACTIONS = ("FOLD", "CHECK", "CALL", "BET", "RAISE", "ALL_IN")
LEARNING_VERSION = "2"
BOARD_STREETS = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}
ROUNDS = {
    "PRE_FLOP": "preflop",
    "BET_BLIND": "preflop",
    "FLOP": "flop",
    "TURN": "turn",
    "RIVER": "river",
}
WARNINGS = [
    "Player aliases are scoped to this Origin and capture source; not persistent account identity",
    "Frequencies are unconditional observations, not a legal policy, hand range or action EV",
    "No VPIP/PFR: dealt-player and action-opportunity denominators are not verified",
    "Decision context is an observation before confirmation, not an engine-verified game state",
]


def initialize(db):
    # NetworkStore owns a larger transaction that also replays immutable receipts.
    # Direct users still get an atomic migration, without committing a caller's work.
    if not db.in_transaction:
        with db:
            db.execute("BEGIN IMMEDIATE")
            _initialize(db)
    else:
        _initialize(db)


def _initialize(db):
    for statement in (
        "CREATE TABLE IF NOT EXISTS capture_learning_metadata "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS capture_learning_heads (origin TEXT, source TEXT, "
        "sequence TEXT NOT NULL, PRIMARY KEY(origin,source))",
        "CREATE TABLE IF NOT EXISTS capture_learning_hands (origin TEXT, source TEXT, hand TEXT, "
        "started INTEGER NOT NULL, complete INTEGER NOT NULL, roster TEXT NOT NULL, "
        "PRIMARY KEY(origin,source,hand))",
        "CREATE TABLE IF NOT EXISTS capture_learning_actions (origin TEXT, source TEXT, hand TEXT, "
        "action_id TEXT, version TEXT, first_sequence TEXT NOT NULL, player TEXT NOT NULL, "
        "street TEXT NOT NULL, quality TEXT NOT NULL, context TEXT NOT NULL, label TEXT NOT NULL, "
        "PRIMARY KEY(origin,source,hand,action_id,version))",
        "CREATE TABLE IF NOT EXISTS capture_learning_counts "
        "(origin TEXT, source TEXT, player TEXT, "
        "street TEXT, quality TEXT, action TEXT, count INTEGER NOT NULL CHECK(count>=0), "
        "PRIMARY KEY(origin,source,player,street,quality,action))",
        "CREATE TABLE IF NOT EXISTS capture_learning_outcomes "
        "(origin TEXT, source TEXT, hand TEXT, "
        "sequence TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(origin,source,hand))",
        "CREATE TABLE IF NOT EXISTS capture_learning_decisions "
        "(origin TEXT, source TEXT, body TEXT NOT NULL, PRIMARY KEY(origin,source))",
    ):
        db.execute(statement)
    row = db.execute(
        "SELECT value FROM capture_learning_metadata WHERE key='schema_version'"
    ).fetchone()
    version = row[0] if row else "1"
    if version not in ("1", LEARNING_VERSION):
        raise ValueError("Unsupported capture learning schema version")
    if version != LEARNING_VERSION:
        # Version 1 could capture post-action cash before its confirmation arrived.
        # Only derived data is retired; NetworkStore rebuilds it in this transaction.
        for name in ("heads", "hands", "actions", "counts", "outcomes", "decisions"):
            db.execute(f"DELETE FROM capture_learning_{name}")
        db.execute(
            "INSERT INTO capture_learning_metadata VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (LEARNING_VERSION,),
        )


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _head(db, origin, source):
    row = db.execute(
        "SELECT sequence FROM capture_learning_heads WHERE origin=? AND source=?", (origin, source)
    ).fetchone()
    return row[0] if row else "0".zfill(20)


def _cutoff(db, origin, source, as_of):
    head = _head(db, origin, source)
    if as_of is None:
        return head
    if isinstance(as_of, str) and as_of.isascii() and as_of.isdecimal() and len(as_of) <= 20:
        as_of = int(as_of)
    if type(as_of) is not int or not 0 <= as_of <= min(U64, int(head)):
        raise ValueError("as_of must be an available nonnegative source sequence")
    return str(as_of).zfill(20)


def _current(db, key):
    return db.execute(
        "SELECT first_sequence,player,street,quality,context,label FROM capture_learning_actions "
        "WHERE origin=? AND source=? AND hand=? AND action_id=? ORDER BY version DESC LIMIT 1",
        key,
    ).fetchone()


def _count(db, scope, player, street, quality, action, delta):
    key = (*scope, player, street, quality, action)
    if delta == 1:
        db.execute(
            "INSERT INTO capture_learning_counts VALUES(?,?,?,?,?,?,1) "
            "ON CONFLICT(origin,source,player,street,quality,action) DO UPDATE SET count=count+1",
            key,
        )
    else:
        db.execute(
            "UPDATE capture_learning_counts SET count=count-1 "
            "WHERE origin=? AND source=? AND player=? AND street=? AND quality=? AND action=?",
            key,
        )


def _decision(db, scope):
    row = db.execute(
        "SELECT body FROM capture_learning_decisions WHERE origin=? AND source=?", scope
    ).fetchone()
    return json.loads(row[0]) if row else None


def _observe_decision(db, scope, event, after):
    """Freeze the first snapshot immediately following an observed turn request."""
    if event.kind not in (
        "action_requested",
        "snapshot",
        "action_confirmed",
        "action_updated",
        "board_updated",
        "hand_started",
        "capture_gap",
        "room_closed",
    ):
        return
    pending = _decision(db, scope)
    if event.kind == "action_requested" and event.data["turn"] is not None:
        player = event.data["turn"]["player_id"]
        if not pending or (pending["hand_id"], pending["player_id"]) != (event.hand_id, player):
            pending = {
                "hand_id": event.hand_id,
                "player_id": player,
                "request_sequence": event.sequence,
                "board": after["board"],
                "snapshot_sequence": None,
                "state": None,
            }
        else:
            return  # Repeated options do not establish a second decision boundary.
    elif pending and event.kind == "snapshot":
        turn = after.get("turn")
        if (
            event.hand_id != pending["hand_id"]
            or not turn
            or turn["player_id"] != pending["player_id"]
            or after["board"] != pending["board"]
            or after["street"] != BOARD_STREETS.get(len(pending["board"]))
            or after["gap"]
            or after["closed"]
        ):
            pending = None
        elif (
            pending["state"] is None and int(event.sequence) == int(pending["request_sequence"]) + 1
        ):
            pending.update(snapshot_sequence=event.sequence, state=after)
        else:
            return  # Never replace the first snapshot with a later cash/card update.
    elif event.kind in (
        "action_confirmed",
        "action_updated",
        "board_updated",
        "hand_started",
        "capture_gap",
        "room_closed",
        "action_requested",
    ):
        pending = None
    if pending:
        db.execute(
            "INSERT INTO capture_learning_decisions VALUES(?,?,?) "
            "ON CONFLICT(origin,source) DO UPDATE SET body=excluded.body",
            (*scope, _json(pending)),
        )
    else:
        db.execute("DELETE FROM capture_learning_decisions WHERE origin=? AND source=?", scope)


def _context(db, scope, event, before, after):
    decision = _decision(db, scope)
    state = decision.get("state") if decision else None
    street = ROUNDS.get(event.data["source_round"])
    valid = (
        event.kind == "action_confirmed"
        and before
        and state
        and decision["hand_id"] == event.hand_id
        and decision["player_id"] == event.data["player_id"]
        and before.get("hand_id") == event.hand_id
        and street is not None
        and state.get("street") == street
        and any(p["player_id"] == event.data["player_id"] for p in state.get("players", []))
        and before.get("street") == street
        and before.get("board") == state.get("board")
        and not before.get("gap")
        and not before.get("closed")
        and not any(
            int(a["source_event_id"]) >= int(event.data["source_event_id"])
            for a in before.get("actions", [])
        )
    )
    state = state if valid else {}
    cutoff = decision["snapshot_sequence"] if valid else str(int(event.sequence) - 1)
    result = {
        "available": bool(valid),
        "observed_street": (before or {}).get("street", "unknown"),
        "observation_cutoff": cutoff,
        "decision_request_sequence": decision["request_sequence"] if valid else None,
        "history_complete": bool(
            valid
            and state.get("history_complete")
            and after.get("history_complete")
            and not state.get("gap")
            and not after.get("gap")
            and not state.get("closed")
        ),
        "board": state.get("board", []),
        "players": state.get("players", []),
        "history_reference": {
            "hand_id": event.hand_id,
            "through_sequence": cutoff,
        }
        if valid
        else None,
        "rules_version": state.get("rules_version"),
    }
    if event.data["player_id"] == "hero":
        result["hero_cards"] = state.get("hero_cards", [None, None])
        result["observed_turn"] = state.get("turn")
    return result


def observe(db, origin, event, before, after):
    """Append only in the caller's receipt/projection transaction; never commits."""
    if not isinstance(event, BusinessEvent):
        raise ValueError("A validated business event is required")
    scope = (origin, event.source_id)
    sequence = event.sequence.zfill(20)
    if sequence <= _head(db, *scope):
        return
    # A source gap invalidates the current projection even if its envelope names an old hand.
    hand = (
        (before or {}).get("hand_id")
        if event.kind == "capture_gap"
        else event.hand_id or (before or {}).get("hand_id")
    )
    if hand is not None:
        key = (*scope, hand)
        row = db.execute(
            "SELECT started,complete,roster FROM capture_learning_hands "
            "WHERE origin=? AND source=? AND hand=?",
            key,
        ).fetchone()
        started, complete, roster = row if row else (0, 0, "[]")
        if event.kind == "hand_started":
            started, complete = 1, int(bool(after.get("history_complete")))
        if after.get("hand_id") == hand:
            complete = int(
                bool(complete and after.get("history_complete") and not after.get("closed"))
            )
            players = sorted(p["player_id"] for p in after.get("players", []))
            if players:
                if roster != "[]" and json.loads(roster) != players:
                    complete = 0
                roster = _json(players)
        db.execute(
            "INSERT INTO capture_learning_hands VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(origin,source,hand) DO UPDATE SET "
            "started=excluded.started,complete=excluded.complete,roster=excluded.roster",
            (*key, started, complete, roster),
        )
        if event.kind == "settlement":
            result = event.data
            total, unknown = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(street='unknown' OR "
                f"quality!='complete_observed_history'),0) FROM ({_latest()}) "
                "WHERE hand=? AND rank=1",
                (*scope, sequence, hand),
            ).fetchone()
            eligible = bool(
                started
                and complete
                and total > 0
                and not unknown
                and result["status"] == "reconciled"
                and sorted(p["player_id"] for p in result["players"]) == json.loads(roster)
            )
            outcome = {
                "quality": "reconciled_observation_only" if eligible else "incomplete_observation",
                "eligible_for_completed_hand_dataset": eligible,
                "players": result["players"],
                "pool_change": result["pool_change"],
                "status": result["status"],
                "reason": result["reason"],
                "warning": "Final hand result, not an action EV or counterfactual reward",
            }
            old_outcome = db.execute(
                "SELECT body FROM capture_learning_outcomes WHERE origin=? AND source=? AND hand=?",
                key,
            ).fetchone()
            if old_outcome:
                saved = json.loads(old_outcome[0])
                if any(
                    saved[k] != outcome[k] for k in ("players", "pool_change", "status", "reason")
                ):
                    raise ValueError("Conflicting learned hand outcome")
            db.execute(
                "INSERT OR IGNORE INTO capture_learning_outcomes VALUES(?,?,?,?,?)",
                (*key, sequence, _json(outcome)),
            )
    if event.kind in ("action_confirmed", "action_updated") and event.data["action"] in ACTIONS:
        key = (*scope, event.hand_id, event.data["source_event_id"])
        old = _current(db, key)
        label = dict(event.data)
        if old:
            previous = json.loads(old[5])
            for field, value in label.items():
                if previous[field] is not None and value is not None and previous[field] != value:
                    raise ValueError("Conflicting learned action identity")
            label = {**previous, **{k: v for k, v in label.items() if v is not None}}
            context, first = json.loads(old[4]), old[0]
        else:
            context, first = _context(db, scope, event, before, after), sequence
        street = ROUNDS.get(label["source_round"], "unknown")
        quality = (
            "unknown_street"
            if street == "unknown"
            else "complete_observed_history"
            if context["history_complete"]
            else "incomplete_history"
        )
        if old is None or old[5] != _json(label):
            if old:
                _count(db, scope, old[1], old[2], old[3], json.loads(old[5])["action"], -1)
            _count(db, scope, label["player_id"], street, quality, label["action"], 1)
            db.execute(
                "INSERT INTO capture_learning_actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    *key,
                    sequence,
                    first,
                    label["player_id"],
                    street,
                    quality,
                    _json(context),
                    _json(label),
                ),
            )
    _observe_decision(db, scope, event, after)
    db.execute(
        "INSERT INTO capture_learning_heads VALUES(?,?,?) ON CONFLICT(origin,source) "
        "DO UPDATE SET sequence=excluded.sequence",
        (*scope, sequence),
    )


def _latest():
    return (
        "SELECT *,ROW_NUMBER() OVER(PARTITION BY hand,action_id ORDER BY version DESC) AS rank "
        "FROM capture_learning_actions WHERE origin=? AND source=? AND version<=?"
    )


def profiles(db, origin, source, as_of=None, *, player_ids=None, limit=64):
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("Profile limit must be 1 to 64")
    cutoff = _cutoff(db, origin, source, as_of)
    if player_ids is not None:
        if not isinstance(player_ids, (list, tuple)) or len(player_ids) > limit:
            raise ValueError("Too many player ids")
        if any(not isinstance(p, str) for p in player_ids):
            raise ValueError("Player ids must be aliases")
        players = [alias(p) for p in player_ids]
        if len(set(players)) != len(players):
            raise ValueError("Duplicate player ids")
    else:
        players = [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT player FROM capture_learning_actions "
                "WHERE origin=? AND source=? AND first_sequence<=? ORDER BY player LIMIT ?",
                (origin, source, cutoff, limit),
            )
        ]
    result = {p: {"player_id": p, "action_observations": 0, "contexts": []} for p in players}
    if players:
        selected = ",".join("?" for _ in players)
        if cutoff == _head(db, origin, source):
            rows = db.execute(
                "SELECT player,street,quality,action,count FROM capture_learning_counts "
                f"WHERE origin=? AND source=? AND player IN ({selected}) AND count>0 "
                "ORDER BY player,street,quality,action",
                (origin, source, *players),
            )
        else:
            rows = db.execute(
                "SELECT player,street,quality,json_extract(label,'$.action'),COUNT(*) "
                f"FROM ({_latest()}) WHERE rank=1 AND player IN ({selected}) "
                "GROUP BY player,street,quality,json_extract(label,'$.action') "
                "ORDER BY player,street,quality",
                (origin, source, cutoff, *players),
            )
        contexts = defaultdict(lambda: dict.fromkeys(ACTIONS, 0))
        for player, street, quality, action, count in rows:
            contexts[player, street, quality][action] = count
        for (player, street, quality), counts in contexts.items():
            n = sum(counts.values())
            result[player]["action_observations"] += n
            result[player]["contexts"].append(
                {
                    "street": street,
                    "quality": quality,
                    "observations": n,
                    "counts": counts,
                    "frequencies": {a: count / n for a, count in counts.items()},
                    "posterior_probabilities": {
                        a: (count + 1) / (n + len(ACTIONS)) for a, count in counts.items()
                    },
                    "prior_per_action": 1.0,
                }
            )
    return {
        "source_id": source,
        "through_sequence": str(int(cutoff)),
        "identity_scope": "origin_source",
        "quality": "observed_behavior_learning",
        "players": list(result.values()),
        "warnings": WARNINGS[:],
    }


def examples(db, origin, source, limit=100, *, as_of=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Example limit must be 1 to 100")
    cutoff = _cutoff(db, origin, source, as_of)
    rows = db.execute(
        "SELECT hand,action_id,first_sequence,version,street,quality,context,label "
        f"FROM ({_latest()}) WHERE rank=1 ORDER BY first_sequence DESC LIMIT ?",
        (origin, source, cutoff, limit),
    ).fetchall()
    results = []
    for hand, action, first, version, street, quality, context, label in rows:
        outcome = db.execute(
            "SELECT sequence,body FROM capture_learning_outcomes "
            "WHERE origin=? AND source=? AND hand=? AND sequence<=?",
            (origin, source, hand, cutoff),
        ).fetchone()
        outcome_label = json.loads(outcome[1]) if outcome else None
        results.append(
            {
                "hand_id": hand,
                "source_event_id": action,
                "observed_sequence": str(int(first)),
                "available_sequence": str(int(version)),
                "street": street,
                "quality": quality,
                "context": json.loads(context),
                "observed_action": json.loads(label),
                "hand_outcome": {
                    "available_sequence": str(int(outcome[0])),
                    **outcome_label,
                }
                if outcome_label and outcome_label["eligible_for_completed_hand_dataset"]
                else None,
            }
        )
    return {
        "origin": origin,
        "source_id": source,
        "through_sequence": str(int(cutoff)),
        "identity_scope": "origin_source",
        "quality": "observed_behavior_learning",
        "examples": results,
        "warnings": WARNINGS
        + [
            "Street-level learning does not consume full action history; history_reference "
            "requires replay of immutable capture_events in this Origin/source through its cutoff",
        ],
    }
