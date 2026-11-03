import json
import sqlite3

import pytest

from texas_analysis.portable_state import export_state, main, restore_state
from texas_game_core.kuhn import KuhnPoker
from texas_strategy.training_store import open_trainer


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "source"
    (root / "core").mkdir(parents=True)
    trainer = open_trainer(KuhnPoker(), root / "core/training.sqlite3")
    trainer.train(3)
    trainer._store.close()
    return root


def test_wal_samples_and_all_training_accumulators_survive_migration(data, tmp_path):
    with sqlite3.connect(data / "capture.sqlite3") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("CREATE TABLE samples (event TEXT)")
        db.execute("INSERT INTO samples VALUES ('synthetic-observed-action')")
        db.commit()
        assert (data / "capture.sqlite3-wal").stat().st_size > 0
        bundle, restored = tmp_path / "bundle", tmp_path / "restored"
        manifest = export_state(data, bundle)
        assert restore_state(bundle, restored) == manifest
    assert not list(restored.rglob("*-wal"))
    with sqlite3.connect(restored / "capture.sqlite3") as db:
        assert db.execute("SELECT * FROM samples").fetchall() == [("synthetic-observed-action",)]
    before = open_trainer(KuhnPoker(), data / "core/training.sqlite3")
    after = open_trainer(KuhnPoker(), restored / "core/training.sqlite3")

    def checkpoint(trainer):
        path = tmp_path / "comparison.json"
        trainer.save_checkpoint(path)
        return json.loads(path.read_text())

    try:
        assert before.iterations == after.iterations == 3
        assert checkpoint(before) == checkpoint(after)
        before.train(1)
        after.train(1)
        assert checkpoint(before) == checkpoint(after)
    finally:
        before._store.close()
        after._store.close()


@pytest.mark.parametrize("failure", ["checksum", "path", "missing", "symlink", "linked_directory"])
def test_invalid_bundle_never_publishes_partial_data(data, tmp_path, failure):
    bundle, target = tmp_path / "bundle", tmp_path / "target"
    export_state(data, bundle)
    model = bundle / "core/training.sqlite3"
    if failure == "checksum":
        with model.open("ab") as handle:
            handle.write(b"corrupt")
    elif failure == "path":
        manifest = json.loads((bundle / "manifest.json").read_text())
        manifest["files"]["../../outside"] = manifest["files"]["core/training.sqlite3"]
        (bundle / "manifest.json").write_text(json.dumps(manifest))
    elif failure == "missing":
        model.unlink()
    elif failure == "symlink":
        model.unlink()
        model.symlink_to(data / "core/training.sqlite3")
    else:
        model.unlink()
        model.parent.rmdir()
        model.parent.symlink_to(data / "core", target_is_directory=True)
    with pytest.raises(ValueError):
        restore_state(bundle, target)
    assert not target.exists() and not list(tmp_path.glob(".texas-restore-*"))


def test_existing_data_is_never_overwritten_and_cli_roundtrip(data, tmp_path, capsys):
    bundle, target = tmp_path / "bundle", tmp_path / "target"
    assert main(["export", "--data-dir", str(data), "--output", str(bundle)]) == 0
    assert main(["restore", "--bundle", str(bundle), "--data-dir", str(target)]) == 0
    assert "sha256" in capsys.readouterr().out
    before = (target / "core/training.sqlite3").read_bytes()
    with pytest.raises(FileExistsError):
        restore_state(bundle, target)
    with pytest.raises(FileExistsError):
        export_state(data, bundle)
    assert (target / "core/training.sqlite3").read_bytes() == before


def test_core_only_cli_export_excludes_capture_and_preserves_training(data, tmp_path):
    with sqlite3.connect(data / "capture.sqlite3") as db:
        db.execute("CREATE TABLE private_events (body TEXT)")
        db.execute("INSERT INTO private_events VALUES ('private capture')")
    bundle, restored = tmp_path / "core-bundle", tmp_path / "core-restored"
    assert main(["export", "--data-dir", str(data), "--output", str(bundle), "--core-only"]) == 0
    manifest = restore_state(bundle, restored)
    assert set(manifest["files"]) == {"core/training.sqlite3"}
    assert not (bundle / "capture.sqlite3").exists()
    assert not (restored / "capture.sqlite3").exists()
    trainer = open_trainer(KuhnPoker(), restored / "core/training.sqlite3")
    try:
        assert trainer.iterations == 3
        assert trainer.train(1).completed_iterations == 1
    finally:
        trainer._store.close()
