"""Observed opponent-policy training, chronological evaluation and atomic release.

Factual human actions train behavior probabilities, not optimal hero actions or
CFR counterfactual values. Models stay inside the capture database/Origin boundary.
"""

import hashlib
import json
import math
from collections import Counter

from texas_strategy.behavior_policy import context, family, fit, predict
from texas_strategy.mushroom_abstraction import context_action_map

from .capture_replay import ReplayUnavailable, _observe_action, replay_capture

VERSION = "public-behavior-v1"
MAX_HANDS = 160
MAX_SAMPLES = 8000
MAX_MODEL_BYTES = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def initialize(db):
    for statement in (
        "CREATE TABLE IF NOT EXISTS capture_strategy_models (origin TEXT, version TEXT, "
        "body TEXT NOT NULL, available_after INTEGER NOT NULL, PRIMARY KEY(origin,version))",
        "CREATE TABLE IF NOT EXISTS capture_strategy_active (origin TEXT PRIMARY KEY, "
        "version TEXT, previous TEXT)",
        "CREATE TABLE IF NOT EXISTS capture_strategy_runs (id INTEGER PRIMARY KEY, origin TEXT, "
        "boundary INTEGER, body TEXT NOT NULL, UNIQUE(origin,boundary))",
    ):
        db.execute(statement)


def load(db, origin):
    row = db.execute(
        "SELECT m.body,m.available_after FROM capture_strategy_models m "
        "JOIN capture_strategy_active a "
        "ON m.origin=a.origin AND m.version=a.version WHERE a.origin=?",
        (origin,),
    ).fetchone()
    if row is None:
        return None
    if len(row[0].encode()) > MAX_MODEL_BYTES + 128:
        raise ValueError("Released behavior model exceeds size budget")
    model = json.loads(row[0])
    if not isinstance(model, dict):
        raise ValueError("Invalid released behavior model")
    expected = model.pop("version", None)
    if (
        model.get("algorithm") != VERSION
        or hashlib.sha256(canonical(model).encode()).hexdigest() != expected
    ):
        raise ValueError("Invalid released behavior model")
    model["version"] = expected
    model["available_after"] = row[1]
    return model


def status(db, origin):
    error = None
    try:
        active = load(db, origin)
    except ValueError:
        current = db.execute(
            "SELECT version FROM capture_strategy_active WHERE origin=?", (origin,)
        ).fetchone()
        active = {"version": current[0]} if current else None
        error = "model_invalid"
    row = db.execute(
        "SELECT body FROM capture_strategy_runs WHERE origin=? ORDER BY id DESC LIMIT 1", (origin,)
    ).fetchone()
    return {
        "active_version": active["version"] if active else None,
        "model_error": error,
        "last_run": json.loads(row[0]) if row else {"status": "waiting_for_completed_hands"},
    }


def dataset(db, origin, check=lambda: None):
    """Bounded extraction; each context is rebuilt from its immutable decision prefix."""
    hands = db.execute(
        "SELECT o.source,o.hand,o.sequence,e.rowid FROM capture_learning_outcomes o "
        "JOIN capture_events e ON e.origin=o.origin AND e.source=o.source "
        "AND e.sequence=o.sequence "
        "WHERE o.origin=? AND json_extract(o.body,'$.eligible_for_completed_hand_dataset')=1 "
        "ORDER BY e.rowid DESC LIMIT ?",
        (origin, MAX_HANDS),
    ).fetchall()[::-1]
    samples, excluded = [], Counter()
    for source, hand, settled, available in hands:
        check()
        start = db.execute(
            "SELECT sequence FROM capture_events WHERE origin=? AND source=? AND sequence<=? "
            "AND json_extract(body,'$.kind')='hand_started' AND json_extract(body,'$.hand_id')=? "
            "ORDER BY sequence DESC LIMIT 1",
            (origin, source, settled, hand),
        ).fetchone()
        if start is None:
            excluded["missing_hand_start"] += 1
            continue
        raw = db.execute(
            "SELECT body FROM capture_events WHERE origin=? AND source=? AND sequence>=? "
            "AND sequence<=? ORDER BY sequence LIMIT 4097",
            (origin, source, start[0], settled),
        ).fetchall()
        if len(raw) > 4096:
            excluded["hand_event_budget"] += 1
            continue
        events = [json.loads(row[0]) for row in raw]
        actions = db.execute(
            "SELECT context,label FROM (SELECT context,label,ROW_NUMBER() OVER (PARTITION BY "
            "action_id ORDER BY version DESC) rank FROM capture_learning_actions "
            "WHERE origin=? AND source=? AND hand=? AND version<=?) WHERE rank=1",
            (origin, source, hand, settled),
        )
        for encoded, label in actions:
            check()
            observed, label = json.loads(encoded), json.loads(label)
            if label["player_id"] == "hero":
                continue
            if not observed["available"] or not observed["history_complete"]:
                excluded["no_decision_boundary"] += 1
                continue
            cutoff = int(observed["observation_cutoff"])
            prefix = [event for event in events if int(event["sequence"]) <= cutoff]
            try:
                replay = replay_capture(prefix)
                actor = replay.game.actor(replay.state)
                if actor in (None, -1) or replay.player_ids[actor] != label["player_id"]:
                    raise ReplayUnavailable("decision_actor_mismatch")
                _observe_action(replay.game, replay.state, label, replay.player_ids)
                actions = tuple(context_action_map(replay.game, replay.state))
                coarse, detailed = context(replay.state, actor, replay.game.button, actions)
                kind = label["action"].lower()
                if kind == "all_in" and label["amount_delta"] <= replay.game.amount_to_call(
                    replay.state
                ):
                    kind = "call"
                outcome = family(kind)
                if outcome not in coarse[2]:
                    raise ReplayUnavailable("label_not_legal")
            except (ReplayUnavailable, ValueError) as error:
                excluded[str(error)[:80]] += 1
                continue
            samples.append(
                {
                    "source": source,
                    "hand": hand,
                    "player": label["player_id"],
                    "action_id": label["source_event_id"],
                    "cutoff": cutoff,
                    "available": available,
                    "coarse": coarse,
                    "detailed": detailed,
                    "label": outcome,
                }
            )
            if len(samples) > MAX_SAMPLES:
                raise ValueError("Behavior dataset exceeds sample budget")
    return samples, dict(excluded), max((h[3] for h in hands), default=0)


def scores(counts, samples):
    loss, brier = 0.0, 0.0
    for sample in samples:
        distribution, _, _ = predict(
            counts, sample["source"], sample["player"], (sample["coarse"], sample["detailed"])
        )
        loss -= math.log(max(1e-12, distribution[sample["label"]]))
        brier += sum((p - (a == sample["label"])) ** 2 for a, p in distribution.items())
    return {"log_loss": loss / len(samples), "brier": brier / len(samples)}


def candidate(samples, *, boundary, excluded, active=None):
    hands = sorted({(s["available"], s["source"], s["hand"]) for s in samples})
    report = {
        "algorithm": VERSION,
        "status": "warming_up",
        "samples": len(samples),
        "hands": len(hands),
        "excluded": excluded,
        "boundary": boundary,
    }
    if len(hands) < 6:
        return report, None
    held_out = max(2, len(hands) // 5)
    while (
        held_out < len(hands) - 4
        and sum(s["available"] >= hands[-held_out][0] for s in samples) < 20
    ):
        held_out += 1
    split = hands[-held_out][0]
    train = [s for s in samples if s["available"] < split]
    test = [s for s in samples if s["available"] >= split]
    # An active model must not have trained/evaluated on any challenger test hand.
    if active and split <= active["boundary"]:
        report["status"] = "waiting_for_new_holdout"
        return report, None
    report.update(train_samples=len(train), test_samples=len(test), split=split)
    if len(train) < 40 or len(test) < 20:
        return report, None
    counts = fit(train)
    comparison = {"candidate": scores(counts, test), "uniform": scores({}, test)}
    if active:
        comparison["active"] = scores(active["counts"], test)
    improved = all(
        comparison["candidate"]["log_loss"] < other["log_loss"] - 0.001
        and comparison["candidate"]["brier"] <= other["brier"]
        for name, other in comparison.items()
        if name != "candidate"
    )
    report.update(status="promoted" if improved else "rejected", evaluation=comparison)
    if not improved:
        return report, None
    # Keep the evaluated weights: do not silently refit with held-out labels at publication.
    model = {
        "algorithm": VERSION,
        "counts": counts,
        "boundary": boundary,
        "split": split,
        "train_samples": len(train),
        "test_samples": len(test),
        "evaluation": comparison,
        "dataset_digest": hashlib.sha256(canonical(samples).encode()).hexdigest(),
    }
    if len(canonical(model).encode()) > MAX_MODEL_BYTES:
        raise ValueError("Behavior model exceeds size budget")
    model["version"] = hashlib.sha256(canonical(model).encode()).hexdigest()
    report["version"] = model["version"]
    return report, model


def publish(db, origin, report, model, *, expected):
    """The caller holds a short write transaction, separate from dataset extraction."""
    active = db.execute(
        "SELECT version FROM capture_strategy_active WHERE origin=?", (origin,)
    ).fetchone()
    if (active[0] if active else None) != expected:
        raise ValueError("Active behavior model changed during evaluation")
    db.execute(
        "INSERT OR IGNORE INTO capture_strategy_runs(origin,boundary,body) VALUES(?,?,?)",
        (origin, report["boundary"], canonical(report)),
    )
    if model is not None:
        available = db.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM capture_events WHERE origin=?", (origin,)
        ).fetchone()[0]
        db.execute(
            "INSERT OR IGNORE INTO capture_strategy_models VALUES(?,?,?,?)",
            (origin, model["version"], canonical(model), available),
        )
        db.execute(
            "INSERT INTO capture_strategy_active VALUES(?,?,?) ON CONFLICT(origin) "
            "DO UPDATE SET previous=version,version=excluded.version",
            (origin, model["version"], expected),
        )
    # Bound derived artifacts, preserving both current and rollback models.
    db.execute(
        "DELETE FROM capture_strategy_runs WHERE origin=? AND id NOT IN "
        "(SELECT id FROM capture_strategy_runs WHERE origin=? ORDER BY id DESC LIMIT 64)",
        (origin, origin),
    )
    db.execute(
        "DELETE FROM capture_strategy_models WHERE origin=? AND version NOT IN "
        "(SELECT version FROM capture_strategy_active WHERE origin=? UNION "
        "SELECT previous FROM capture_strategy_active WHERE origin=? AND previous IS NOT NULL)",
        (origin, origin, origin),
    )


def rollback(db, origin, expected):
    row = db.execute(
        "SELECT version,previous FROM capture_strategy_active WHERE origin=?", (origin,)
    ).fetchone()
    if row is None or row[0] != expected or row[1] is None:
        raise ValueError("No matching behavior model rollback is available")
    db.execute(
        "UPDATE capture_strategy_active SET version=?,previous=? WHERE origin=?",
        (row[1], row[0], origin),
    )
    load(db, origin)
    return row[1]
