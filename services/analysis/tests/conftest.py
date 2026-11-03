import pytest


@pytest.fixture(autouse=True)
def disable_unrelated_background_training(monkeypatch):
    # Worker integration tests explicitly opt in; ordinary API tests stay isolated.
    monkeypatch.setenv("TEXAS_AUTO_TRAIN", "0")
