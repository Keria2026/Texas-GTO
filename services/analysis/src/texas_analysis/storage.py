"""Transactional event sourcing for explicitly synthetic sessions.

Only seed/config and immutable actions (including chance) are persisted. Reads
rebuild state through the same rule engine; clients never submit hidden states.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from random import Random
from uuid import UUID, uuid4

from texas_game_core.interfaces import CHANCE_PLAYER

from .game_factory import make_game
from .models import CreateSession, GameConfig, SessionResponse, SubmitAction


class ConflictError(ValueError):
    pass


class SessionStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, config TEXT NOT NULL, seed INTEGER NOT NULL,
                  rules_version TEXT);
                CREATE TABLE IF NOT EXISTS events (
                  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                  seq INTEGER NOT NULL, action TEXT NOT NULL,
                  PRIMARY KEY(session_id,seq));
                CREATE TABLE IF NOT EXISTS requests (
                  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                  event_id TEXT NOT NULL, payload TEXT NOT NULL, response TEXT NOT NULL,
                  PRIMARY KEY(session_id,event_id));
            """)
            if "rules_version" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN rules_version TEXT")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _read(self, db, session_id):
        UUID(session_id)
        record = db.execute(
            "SELECT config,seed,rules_version FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if record is None:
            raise KeyError("Unknown session")
        config = GameConfig.model_validate_json(record[0])
        game = make_game(config)
        if record[2] is None and config.game_id == "mushroom":
            raise ConflictError("Legacy mushroom session has no rule version; create a new session")
        if record[2] is not None and record[2] != game.rules_version:
            raise ConflictError("Saved session uses different rules; create a new session")
        state = game.initial_state()
        events = db.execute(
            "SELECT action FROM events WHERE session_id=? ORDER BY seq", (session_id,)
        ).fetchall()
        for (action,) in events:
            state = game.transition(state, action)
        return config, record[1], game, state, len(events)

    def load(self, session_id):
        with self.connection() as db:
            db.execute("BEGIN")
            return self._read(db, session_id)

    def version(self, session_id):
        """Read the committed event position without replaying the game."""
        UUID(session_id)
        with self.connection() as db:
            row = db.execute(
                "SELECT (SELECT COALESCE(MAX(seq)+1,0) FROM events WHERE session_id=s.id) "
                "FROM sessions AS s WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError("Unknown session")
        return row[0]

    @staticmethod
    def _snapshot(session_id, config, game, state, version, viewer=None):
        actor = game.actor(state)
        if actor == CHANCE_PLAYER:
            raise RuntimeError("Unresolved chance state")
        if viewer is None:
            viewer = actor if actor is not None else 0
        if not 0 <= viewer < game.num_players:
            raise ValueError("viewer is not a participating seat")
        return SessionResponse(
            session_id=session_id,
            state_version=version,
            game=config,
            rules_version=game.rules_version,
            actor=actor,
            terminal=actor is None,
            legal_actions=list(game.legal_actions(state)) if actor is not None else [],
            observation=game.observation(state, viewer),
            payoffs=list(map(float, game.terminal_payoffs(state))) if actor is None else None,
        )

    def _deal(self, db, session_id, seed, game, state, version):
        while game.actor(state) == CHANCE_PLAYER:
            outcomes = game.chance_outcomes(state)
            rng = Random(f"{seed}:{version}")
            action = rng.choices([a for a, _ in outcomes], [p for _, p in outcomes])[0]
            state = game.transition(state, action)
            db.execute("INSERT INTO events VALUES (?,?,?)", (session_id, version, action))
            version += 1
        return state, version

    def create(self, request: CreateSession):
        session_id = str(uuid4())
        game = make_game(request.game)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO sessions (id,config,seed,rules_version) VALUES (?,?,?,?)",
                (session_id, request.game.model_dump_json(), request.seed, game.rules_version),
            )
            state, version = self._deal(db, session_id, request.seed, game, game.initial_state(), 0)
            return self._snapshot(session_id, request.game, game, state, version)

    def snapshot(self, session_id, viewer=None):
        config, _, game, state, version = self.load(session_id)
        return self._snapshot(session_id, config, game, state, version, viewer)

    def act(self, session_id, request: SubmitAction):
        payload = request.model_dump_json()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            config, seed, game, state, version = self._read(db, session_id)
            prior = db.execute(
                "SELECT payload,response FROM requests WHERE session_id=? AND event_id=?",
                (session_id, request.event_id),
            ).fetchone()
            if prior is not None:
                if json.loads(prior[0]) != json.loads(payload):
                    raise ConflictError("event_id was already used with different data")
                return SessionResponse.model_validate_json(prior[1])
            if request.expected_version != version:
                raise ConflictError("State version changed; refresh before acting")
            if game.actor(state) is None:
                raise ConflictError("Session is already terminal")
            # Holdem transition validates all integer raise_to values, including off-tree sizes.
            state = game.transition(state, request.action)
            db.execute("INSERT INTO events VALUES (?,?,?)", (session_id, version, request.action))
            state, version = self._deal(db, session_id, seed, game, state, version + 1)
            response = self._snapshot(session_id, config, game, state, version)
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?)",
                (session_id, request.event_id, payload, response.model_dump_json()),
            )
            return response

    def public_profiles(self, session_id, as_of=None):
        from .session_profiles import summarize_profiles

        # Config and events must come from the same SQLite snapshot.
        with self.connection() as db:
            db.execute("BEGIN")
            _, _, game, _, _ = self._read(db, session_id)
            actions = [
                row[0]
                for row in db.execute(
                    "SELECT action FROM events WHERE session_id=? ORDER BY seq", (session_id,)
                )
            ]
            return summarize_profiles(game, actions, session_id=session_id, as_of=as_of)

    def delete(self, session_id):
        UUID(session_id)
        with self.connection() as db:
            if db.execute("DELETE FROM sessions WHERE id=?", (session_id,)).rowcount == 0:
                raise KeyError("Unknown session")
