import copy
import sqlite3

import pytest

from texas_analysis import capture_strategy as learning
from texas_game_core.public_holdem import HoldemRules, PublicHoldemGame
from texas_strategy.behavior_policy import BehaviorPolicy, context, fit, predict
from texas_strategy.policy import PolicyEntry


def samples(hands=10, label="passive", start=0):
    game = PublicHoldemGame(
        HoldemRules((800,) * 8, small_blind=2, big_blind=4, straddle=8, button=0)
    )
    state = game.initial_state()
    coarse, detailed = context(state, game.actor(state), game.button, ("fold", "call", "raise_min"))
    return [
        {
            "source": "source-1",
            "player": "player-1",
            "hand": str(h),
            "action_id": str(i),
            "cutoff": h * 100 + i,
            "available": start + h * 100 + 99,
            "coarse": coarse,
            "detailed": detailed,
            "label": label,
        }
        for h in range(hands)
        for i in range(10)
    ]


def database():
    db = sqlite3.connect(":memory:")
    learning.initialize(db)
    db.execute("CREATE TABLE capture_events (origin TEXT)")
    return db


def test_temporal_whole_hand_split_model_is_not_refitted_on_holdout():
    data = samples()
    report, model = learning.candidate(data, boundary=999, excluded={})
    assert report["status"] == "promoted"
    assert model["train_samples"] == 80 and model["test_samples"] == 20
    assert model["counts"] == fit(data[:80])
    changed = copy.deepcopy(data)
    for row in changed[80:]:
        row["label"] = "raise"
    report, rejected = learning.candidate(changed, boundary=999, excluded={})
    assert report["status"] == "rejected" and rejected is None
    report, _ = learning.candidate(data, boundary=999, excluded={}, active=model)
    assert report["status"] == "waiting_for_new_holdout"


def test_atomic_origin_isolation_rejection_compare_and_swap_and_rollback():
    db = database()
    report, model = learning.candidate(samples(), boundary=999, excluded={})
    learning.publish(db, "A", report, model, expected=None)
    assert learning.load(db, "B") is None
    old = learning.load(db, "A")["version"]
    learning.publish(db, "A", {"boundary": 1000, "status": "rejected"}, None, expected=old)
    assert learning.load(db, "A")["version"] == old
    report2, model2 = learning.candidate(
        samples(start=1000, label="raise"), boundary=1999, excluded={}, active=model
    )
    assert report2["status"] == "promoted"
    with pytest.raises(ValueError, match="changed"):
        learning.publish(db, "A", report2, model2, expected=None)
    learning.publish(db, "A", report2, model2, expected=old)
    assert learning.rollback(db, "A", model2["version"]) == old
    assert learning.load(db, "A")["version"] == old
    assert db.execute("SELECT COUNT(*) FROM capture_strategy_models").fetchone()[0] == 2
    with pytest.raises(ValueError, match="rollback"):
        learning.rollback(db, "B", old)


def test_scoped_personalization_and_legal_support():
    data = samples()
    counts = fit(data)
    p, personal, _ = predict(
        counts, "source-1", "player-1", (data[0]["coarse"], data[0]["detailed"])
    )
    assert personal == 100 and p["passive"] > 0.95
    p, personal, _ = predict(
        counts, "source-2", "player-1", (data[0]["coarse"], data[0]["detailed"])
    )
    assert personal == 0
    assert sum(p.values()) == pytest.approx(1)
    assert set(p) == {"fold", "passive", "raise"}


def test_trained_behavior_changes_opponent_response_but_never_hero():
    game = PublicHoldemGame(
        HoldemRules((800,) * 8, small_blind=2, big_blind=4, straddle=8, button=0)
    )
    state = game.initial_state()
    actor = game.actor(state)

    class Blueprint:
        metadata = {}

    players = tuple("player-1" if i == actor else f"player-{i + 2}" for i in range(8))
    policy = BehaviorPolicy(
        Blueprint(), fit(samples()), source="source-1", players=players, hero=0, button=0
    )
    entry = PolicyEntry(("fold", "call", "raise_min"), (1 / 3, 1 / 3, 1 / 3))
    changed = policy.public_entry(entry, actor, state, entry.actions)
    assert changed.probabilities[1] > entry.probabilities[1]
    assert policy.public_entry(entry, 0, state, entry.actions) is entry
    assert 0 < policy.max_weight <= 0.25
    assert sum(changed.probabilities) == pytest.approx(1)


def test_corrupt_published_model_is_reported_and_does_not_become_a_valid_policy():
    db = database()
    report, model = learning.candidate(samples(), boundary=999, excluded={})
    learning.publish(db, "A", report, model, expected=None)
    db.execute("UPDATE capture_strategy_models SET body='{}'")
    with pytest.raises(ValueError, match="Invalid"):
        learning.load(db, "A")
    assert learning.status(db, "A")["model_error"] == "model_invalid"
