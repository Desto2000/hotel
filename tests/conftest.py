import pytest


@pytest.fixture(autouse=True)
def no_model_downloads(monkeypatch):
    """Tests never download the embedding model; tests that need vectors inject a fake embedder."""
    monkeypatch.setenv("HOTEL_EMBEDDER", "none")
