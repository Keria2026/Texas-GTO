"""Portable committed training and WPK-learning snapshots, separate from application code."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from texas_strategy.training_store import _header, backup_checkpoint

FILES = ("core/training.sqlite3", "capture.sqlite3")


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _validate_database(path, *, training=False):
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA cache_size=-8192")
        db.execute("PRAGMA mmap_size=0")
        if training:
            _header(db)
        elif db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("Corrupt capture database")


def _new_destination(path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Destination must be new; existing data is preserved: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def export_state(data_dir, output, *, core_only=False):
    """Each SQLite backup includes its committed WAL; no live main-file copies."""
    source, output = Path(data_dir), _new_destination(output)
    if not (source / FILES[0]).is_file():
        raise ValueError("No committed core training database to export")
    with TemporaryDirectory(prefix=".texas-export-", dir=output.parent) as temporary:
        stage = Path(temporary) / "bundle"
        stage.mkdir(mode=0o700)
        manifest = {"schema_version": 1, "files": {}}
        for name in FILES[:1] if core_only else FILES:
            original, target = source / name, stage / name
            if not original.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if name == FILES[0]:
                backup_checkpoint(original, target)
            else:
                with closing(
                    sqlite3.connect(original.absolute().as_uri() + "?mode=ro", uri=True)
                ) as db:
                    db.execute("PRAGMA cache_size=-8192")
                    db.execute("PRAGMA mmap_size=0")
                    with closing(sqlite3.connect(target)) as destination:
                        db.backup(destination, pages=256)
                        destination.execute("PRAGMA journal_mode=DELETE")
                _validate_database(target)
            target.chmod(0o600)
            manifest["files"][name] = _digest(target)
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        os.rename(stage, output)
    return manifest


def restore_state(bundle, data_dir):
    """Validate in a staging directory, then publish only to a new data location."""
    bundle, target = Path(bundle), _new_destination(data_dir)
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 16 * 1024:
        raise ValueError("Invalid state manifest")
    manifest = json.loads(manifest_path.read_text())
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "files"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or not isinstance(manifest["files"], dict)
        or FILES[0] not in manifest["files"]
        or set(manifest["files"]) - set(FILES)
    ):
        raise ValueError("Unsupported state bundle")
    with TemporaryDirectory(prefix=".texas-restore-", dir=target.parent) as temporary:
        stage = Path(temporary) / "data"
        stage.mkdir(mode=0o700)
        for name, expected in manifest["files"].items():
            original, copied = bundle / name, stage / name
            if (
                original.is_symlink()
                or not original.is_file()
                or not original.resolve().is_relative_to(bundle.resolve())
            ):
                raise ValueError("Bundle must contain regular database files")
            if (
                not isinstance(expected, dict)
                or set(expected) != {"bytes", "sha256"}
                or type(expected["bytes"]) is not int
                or expected["bytes"] < 1
                or not isinstance(expected["sha256"], str)
                or len(expected["sha256"]) != 64
            ):
                raise ValueError("Invalid database digest")
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, copied)
            copied.chmod(0o600)
            if _digest(copied) != expected:
                raise ValueError(f"Database checksum mismatch: {name}")
            _validate_database(copied, training=name == FILES[0])
        os.rename(stage, target)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--data-dir", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument(
        "--core-only", action="store_true", help="Exclude captured events and profiles"
    )
    restore = commands.add_parser("restore")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--data-dir", type=Path, required=True)
    compact = commands.add_parser("compact", help="Stop service first; keep only current rule rows")
    compact.add_argument("--data-dir", type=Path, required=True)
    compact.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "compact":
            from .compact_state import compact_state

            result = compact_state(args.data_dir, args.backup)
            print(json.dumps(result, indent=2))
            return 0
        result = (
            export_state(args.data_dir, args.output, core_only=args.core_only)
            if args.command == "export"
            else restore_state(args.bundle, args.data_dir)
        )
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))
    return 0
