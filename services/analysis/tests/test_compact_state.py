import fcntl
import sqlite3
from contextlib import closing

import pytest

from texas_analysis.compact_state import compact_state
from texas_analysis.core_training import _open_core_trainer
from texas_strategy.cfr import _TableEntry
from texas_strategy.training_store import open_trainer


def test_compact_keeps_current_values_rng_and_resumability(tmp_path):
    core = tmp_path / "data" / "core"
    core.mkdir(parents=True)
    trainer = _open_core_trainer(core)
    store = trainer._store
    store.begin(0)
    store.set_entry((0, "legacy"), _TableEntry(("fold", "call"), [1.0, -1.0], [2.0, 3.0]), 0)
    store.set_entry((0, "ante4:current"), _TableEntry(("fold", "call"), [3.0, -2.0], [0.0, 0.0]), 0)
    store.commit(trainer, 1)
    document, game = store._document, trainer.game
    store.close()
    source = core / "training.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        row = db.execute(
            "SELECT * FROM training_entries WHERE information_key='ante4:current'"
        ).fetchone()
    backup = tmp_path / "backup.sqlite3"
    with (core / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="Stop"):
            compact_state(tmp_path / "data", backup)
    result = compact_state(tmp_path / "data", backup)
    assert (result["removed"], result["retained"]) == (1, 1)
    with closing(sqlite3.connect(source)) as db:
        assert db.execute("SELECT * FROM training_entries").fetchall() == [row]
    with closing(sqlite3.connect(backup)) as db:
        assert db.execute("SELECT count(*) FROM training_entries").fetchone()[0] == 2
    restored = open_trainer(game, source)
    try:
        assert restored._store._document == document
        assert restored.iterations == 1
        assert restored._store.counts() == (1, 0)
        # A bounded cancelled iteration must retain the compacted committed state.
        restored.train(1, max_nodes=0)
        assert restored._store._document == document
    finally:
        restored._store.close()
