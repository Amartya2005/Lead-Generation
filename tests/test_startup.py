"""Cold-start robustness on Vercel: simultaneous instances must not break each other's startup,
and a failed startup must be retried instead of serving the error page for the instance's lifetime."""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest
from flask import Flask
from werkzeug.test import Client

from tests.conftest import PG_URL

ROOT = Path(__file__).resolve().parent.parent


def _stub_pg_db(error: Exception | None):
    from app.db import DB

    d = object.__new__(DB)
    d.pg, d.sql = True, []

    def execute(q, args=()):
        d.sql.append(q)
        if error:
            raise error
    d.execute = execute
    return d


def test_lock_down_is_one_serialized_statement():
    d = _stub_pg_db(None)
    d._lock_down_api_access()
    assert len(d.sql) == 1
    sql = d.sql[0]
    assert "pg_advisory_xact_lock" in sql and "ENABLE ROW LEVEL SECURITY" in sql and "REVOKE ALL ON leads" in sql


def test_lock_down_tolerates_losing_a_race():
    _stub_pg_db(Exception("tuple concurrently updated"))._lock_down_api_access()  # no raise
    with pytest.raises(Exception, match="permission denied"):
        _stub_pg_db(Exception("permission denied for table leads"))._lock_down_api_access()


@pytest.mark.skipif(not PG_URL, reason="needs TEST_DATABASE_URL (Postgres)")
def test_simultaneous_cold_starts_on_postgres(db):
    from app.db import DB

    errors, barrier = [], threading.Barrier(8)

    def start():
        barrier.wait()
        try:
            DB(PG_URL)
        except Exception as e:  # pragma: no cover - the regression
            errors.append(e)

    for _ in range(3):
        threads = [threading.Thread(target=start) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert errors == []


def _load_entry_point(monkeypatch, create_app):
    import app.web

    monkeypatch.setattr(app.web, "create_app", create_app)
    spec = importlib.util.spec_from_file_location("vercel_entry", ROOT / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_entry_point_retries_a_failed_startup(monkeypatch):
    calls = []

    def create_app():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("tuple concurrently updated")
        ok = Flask("ok")
        ok.add_url_rule("/healthz", "healthz", lambda: "ok")
        return ok

    mod = _load_entry_point(monkeypatch, create_app)
    client = Client(mod.app)
    assert client.get("/healthz").status_code == 503  # inside the retry window: setup page
    assert len(calls) == 1
    monkeypatch.setattr(mod, "RETRY_SECONDS", 0)
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_data(as_text=True) == "ok"  # recovered without a redeploy
    client.get("/healthz")
    assert len(calls) == 2  # built once; no rebuild after success


def test_entry_point_keeps_the_setup_hint(monkeypatch):
    def create_app():
        raise RuntimeError("DATABASE_URL is not set: Vercel has no persistent disk")

    mod = _load_entry_point(monkeypatch, create_app)
    r = Client(mod.app).get("/")
    assert r.status_code == 503 and "add DATABASE_URL" in r.get_data(as_text=True)
