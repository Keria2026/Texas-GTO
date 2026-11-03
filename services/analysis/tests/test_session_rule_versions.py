import sqlite3
from uuid import uuid4

import pytest

from texas_analysis.core_training import CORE_GAME
from texas_analysis.models import CreateSession, GameConfig
from texas_analysis.storage import ConflictError, SessionStore


@pytest.mark.parametrize(
    "abstraction,prefix",
    [("none", "mushroom-session-horizon-v2:"), ("mushroom_context", "mushroom-context-v1:")],
)
def test_saved_session_cannot_reinterpret_its_history_after_a_rule_change(
    tmp_path, abstraction, prefix
):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    config = CORE_GAME.model_copy(update={"abstraction": abstraction})
    created = store.create(CreateSession(game=config, seed=42))
    assert created.rules_version.startswith(prefix)
    with store.connection() as db:
        events = db.execute("SELECT * FROM events").fetchall()
        db.execute("UPDATE sessions SET rules_version='retired-rules'")
    with pytest.raises(ConflictError, match="different rules"):
        store.load(created.session_id)
    with store.connection() as db:
        assert db.execute("SELECT * FROM events").fetchall() == events


def test_legacy_database_adds_version_without_guessing_old_mushroom_rules(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    mushroom_id, kuhn_id = str(uuid4()), str(uuid4())
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, config TEXT, seed INTEGER)")
        db.executemany(
            "INSERT INTO sessions VALUES (?,?,?)",
            [
                (mushroom_id, CORE_GAME.model_dump_json(), 42),
                (kuhn_id, GameConfig().model_dump_json(), 42),
            ],
        )
    store = SessionStore(path)
    assert store.load(kuhn_id)[0].game_id == "kuhn"
    with pytest.raises(ConflictError, match="no rule version"):
        store.load(mushroom_id)
    current = store.create(CreateSession(game=CORE_GAME))
    assert store.snapshot(current.session_id).rules_version == current.rules_version
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 3
