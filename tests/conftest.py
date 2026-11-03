"""Shared fixtures. Live-service tests need the seeded database and trained forecast models; the `seeded` fixture
builds them once (python -m restaurant_ops.data.seed) when data/restaurant.db is missing."""
from __future__ import annotations

import pytest

from restaurant_ops.config import settings


@pytest.fixture(scope="session", autouse=True)
def private_master(tmp_path_factory):
    """Run the whole suite on a copy of the master DB, so tests never write users, audit rows, gift cards or forecast
    adjustments into the real data/restaurant.db. The trained models (MODELS_DIR) are only read."""
    import sqlite3

    real = settings.DB_PATH
    if not real.exists():
        from restaurant_ops.data import seed

        assert seed.main([]) == 0
    copy = tmp_path_factory.mktemp("master") / "restaurant.db"
    src, dst = sqlite3.connect(real), sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    old = settings.DB_PATH
    settings.DB_PATH = copy
    yield copy
    settings.DB_PATH = old


@pytest.fixture(autouse=True)
def fresh_carry_over(private_master):
    """Each test starts from the same world: drop what earlier tests' services carried into the master for later
    services (live adjust_forecast decisions, saved prep plans)."""
    yield
    import sqlite3

    c = sqlite3.connect(settings.DB_PATH)
    try:
        c.execute("DELETE FROM decisions WHERE run_id LIKE 'svc-%'")
        c.execute("DELETE FROM config_kv WHERE key LIKE 'prep_plan:%'")
        c.commit()
    except sqlite3.Error:
        pass
    finally:
        c.close()


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


def token(username: str = "manager", password: str | None = None) -> str:
    """A session token for a user in the master DB (the demo users manager / server / kitchen exist after bootstrap)."""
    from restaurant_ops import admin as ADMIN
    from restaurant_ops.db.connection import get_conn, init_db
    from restaurant_ops.live import auth as AUTH

    c = get_conn(settings.DB_PATH)
    try:
        init_db(c)
        ADMIN.seed_defaults(c)
        AUTH.bootstrap(c)
        tok, _ = AUTH.login(c, username, password or f"{username}-demo")
        return tok
    finally:
        c.close()


def bearer(username: str = "manager") -> dict:
    return {"Authorization": f"Bearer {token(username)}"}


ANON = {"Authorization": ""}
