"""Independent regressions for API/storage review findings."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from texas_analysis import storage
from texas_analysis.models import CreateSession
from texas_analysis.storage import SessionStore


def test_snapshot_config_and_events_share_transaction_during_concurrent_delete(
    tmp_path, monkeypatch
):
    store = SessionStore(tmp_path / "race.sqlite3")
    initial = store.create(CreateSession())
    deleting = Event()
    deleted = Event()
    original_make_game = storage.make_game

    def delete_session():
        deleting.wait(2)
        store.delete(initial.session_id)
        deleted.set()

    def between_config_and_events(config):
        deleting.set()
        # Without an explicit read transaction DELETE commits here, and the
        # following event query incorrectly sees an empty, fresh game. With a
        # read transaction DELETE waits until the coherent snapshot is read.
        deleted.wait(0.15)
        return original_make_game(config)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(delete_session)
        monkeypatch.setattr(storage, "make_game", between_config_and_events)
        try:
            snapshot = store.snapshot(initial.session_id)
            assert snapshot == initial
        finally:
            deleting.set()
            future.result(timeout=3)
    assert deleted.is_set()
