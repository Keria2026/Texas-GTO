"""Exercise the shipped wheel offline, outside source imports and without user services."""

import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path


def probe(prefix):
    import sqlite3

    import texas_analysis
    from texas_game_core.kuhn import KuhnPoker
    from texas_strategy.cfr import Trainer
    from texas_strategy.training_store import open_trainer

    assert Path(texas_analysis.__file__).is_relative_to(prefix)
    source = prefix / "data"
    (source / "core").mkdir()
    seed = prefix / "mixed-seed.json"
    Trainer(
        KuhnPoker(),
        algorithm="pluribus",
        pluribus={"average_mode": "sampled_mixture", "average_samples": 4},
    ).save_checkpoint(seed)
    first = open_trainer(KuhnPoker(), source / "core/training.sqlite3", checkpoint=seed)
    first.train(3)
    try:
        with sqlite3.connect(source / "capture.sqlite3") as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE synthetic_samples (event TEXT)")
            db.execute("INSERT INTO synthetic_samples VALUES ('public-action')")
            db.commit()
            exported = prefix / "state-export"
            subprocess.run(
                [
                    str(prefix / "current/bin/texas-state"),
                    "export",
                    "--data-dir",
                    str(source),
                    "--output",
                    str(exported),
                ],
                check=True,
            )
        restored = prefix / "state-restored"
        subprocess.run(
            [
                str(prefix / "current/bin/texas-state"),
                "restore",
                "--bundle",
                str(exported),
                "--data-dir",
                str(restored),
            ],
            check=True,
        )
        second = open_trainer(KuhnPoker(), restored / "core/training.sqlite3")
        try:
            first.train(1)
            second.train(1)
            first.save_checkpoint(prefix / "first.json")
            second.save_checkpoint(prefix / "second.json")
            assert json.loads((prefix / "first.json").read_text()) == json.loads(
                (prefix / "second.json").read_text()
            )
        finally:
            second._store.close()
        with sqlite3.connect(restored / "capture.sqlite3") as db:
            assert db.execute("SELECT * FROM synthetic_samples").fetchall() == [("public-action",)]
    finally:
        first._store.close()


def main(archive):
    environment = dict(os.environ, TEXAS_INSTALL_NO_SERVICE="1")
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="texas-release-") as temporary:
        directory = Path(temporary)
        with tarfile.open(archive) as package:
            package.extractall(directory, filter="data")
        prefix = directory / "installed"
        installer = str(directory / "Serve/install.sh")
        subprocess.run(["bash", installer, str(prefix)], env=environment, check=True)
        python = str(prefix / "current/bin/python")
        subprocess.run(
            [python, "-I", str(Path(__file__).resolve()), "--probe", str(prefix)],
            env=environment,
            cwd=directory,
            check=True,
        )
        retained = (prefix / "data/core/training.sqlite3").read_bytes()
        subprocess.run(["bash", installer, str(prefix)], env=environment, check=True)
        assert (prefix / "data/core/training.sqlite3").read_bytes() == retained
        second_install = directory / "migrated-install"
        subprocess.run(
            ["bash", installer, str(second_install), str(prefix / "state-export")],
            env=environment,
            check=True,
        )
        assert (second_install / "data/capture.sqlite3").is_file()
        environment.update(
            TEXAS_DATA_DIR=str(prefix / "data"), TEXAS_PORT="0", TEXAS_AUTO_TRAIN="0"
        )
        log = directory / "server.log"
        with log.open("w") as handle:
            process = subprocess.Popen(
                [python, "-I", "-m", "texas_analysis"],
                env=environment,
                cwd=directory,
                stdout=handle,
                stderr=handle,
            )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(log.read_text())
                address = re.search(r"Uvicorn running on http://127.0.0.1:(\d+)", log.read_text())
                if address:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{address[1]}/v1/health", headers={"X-Texas-Local": "1"}
                    )
                    with urllib.request.urlopen(request, timeout=3) as response:
                        assert response.status == 200
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("Installed service did not start: " + log.read_text())
        finally:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        print(
            json.dumps(
                {
                    "offline_install": True,
                    "upgrade_retains_data": True,
                    "state_migration_and_resume": True,
                    "installed_http_service": True,
                }
            )
        )


if __name__ == "__main__":
    if sys.argv[1] == "--probe":
        probe(Path(sys.argv[2]))
    else:
        main(Path(sys.argv[1]).resolve())
