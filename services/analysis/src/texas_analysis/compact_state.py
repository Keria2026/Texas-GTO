"""Offline, lossless removal of policy rows outside the current rule namespace."""

import fcntl
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from texas_strategy.mushroom_population import MushroomPopulationGame
from texas_strategy.training_store import _SCHEMA, _header


def compact_state(data_dir, backup):
    directory, backup = Path(data_dir) / "core", Path(backup).absolute()
    source = directory / "training.sqlite3"
    if backup.exists() or backup.is_symlink():
        raise FileExistsError("Backup destination already exists")
    if not source.is_file():
        raise ValueError("No training database")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with (directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Stop the algorithm service before compacting") from error
        with closing(sqlite3.connect(source, timeout=0)) as db:
            db.execute("PRAGMA cache_size=-8192")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # DELETE mode requires all live WAL readers to leave before replacement.
            db.execute("PRAGMA journal_mode=DELETE")
            document = _header(db)
            game = MushroomPopulationGame.from_context(document["training_context"])
            prefix = game.training_key_prefix
            if game.ante != 4 or prefix != "ante4:":
                raise ValueError("Compaction requires the current ante4 population")
            total = db.execute("SELECT count(*) FROM training_entries").fetchone()[0]
            with TemporaryDirectory(prefix=".compact-", dir=directory) as temporary:
                target = Path(temporary) / "training.sqlite3"
                with closing(sqlite3.connect(target)) as new:
                    new.execute("PRAGMA cache_size=-8192")
                    new.execute("PRAGMA user_version=1")
                    for statement in _SCHEMA:
                        new.execute(statement)
                    new.execute("ATTACH DATABASE ? AS old", (str(source),))
                    new.execute("INSERT INTO training_meta SELECT * FROM old.training_meta")
                    new.execute(
                        "INSERT INTO training_entries SELECT * FROM old.training_entries "
                        "WHERE substr(information_key,1,?)=?",
                        (len(prefix), prefix),
                    )
                    new.commit()
                    kept = new.execute("SELECT count(*) FROM training_entries").fetchone()[0]
                    if _header(new) != document:
                        raise ValueError("Training metadata changed during compaction")
                    # Exact comparison includes row IDs, regrets and average accumulators.
                    if new.execute(
                        "SELECT 1 FROM (SELECT * FROM training_entries EXCEPT "
                        "SELECT * FROM old.training_entries) LIMIT 1"
                    ).fetchone():
                        raise ValueError("Retained training values changed")
                target.chmod(0o600)
                # Same-filesystem hard link is a complete rollback copy, without another 6 GB write.
                os.link(source, backup)
                os.replace(target, source)
    (directory / "progress.json").unlink(missing_ok=True)
    return {
        "removed": total - kept,
        "retained": kept,
        "backup": str(backup),
        "iterations": document["metadata"]["iterations"],
        "bytes": source.stat().st_size,
    }
