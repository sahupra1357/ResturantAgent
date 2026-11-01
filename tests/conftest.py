"""Shared fixtures. Live-service tests need the seeded database and trained forecast models; the `seeded` fixture
builds them once (python -m restaurant_ops.data.seed) when data/restaurant.db is missing."""
from __future__ import annotations

import pytest

from restaurant_ops.config import settings


@pytest.fixture(scope="session")
def seeded():
    if not settings.DB_PATH.exists():
        from restaurant_ops.data import seed

        assert seed.main([]) == 0
    from restaurant_ops.core import forecasting
    from restaurant_ops.db.connection import get_conn

    c = get_conn(settings.DB_PATH)
    try:
        forecasting.load_models(c)
    finally:
        c.close()
    return settings.DB_PATH


@pytest.fixture()
def live_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LIVE_DIR", tmp_path / "live")
    return settings.LIVE_DIR
