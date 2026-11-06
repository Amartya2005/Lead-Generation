"""Serverless tick: bounded steps, one email per tick, spacing, endpoint auth, stuck-send recovery."""
from __future__ import annotations

import functools
import http.server
import smtplib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


class FakeSMTP:
    sent: list = []

    def __init__(self, *a, **k): ...
    def ehlo(self): ...
    def has_extn(self, name): return False
    def login(self, u, p): ...
    def quit(self): ...
    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture()
def site():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIX / "site_old"))
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]
    srv = http.server.ThreadingHTTPServer(("localhost", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_address[1]}/"
    srv.shutdown()


@pytest.fixture()
def tcfg(cfg, monkeypatch):
    cfg.settings["research"]["queries_per_run"] = 0          # no live web search in tests
    cfg.settings["sending"].update(min_delay_seconds=90, max_delay_seconds=90)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    return cfg


def _approve_all(db):
    for r in db.leads_by_status("drafted"):
        db.update_lead(r["id"], status="approved")


def test_tick_moves_a_lead_through_the_pipeline_then_sends_one(tcfg, db, site):
    from app.graph import Agents
    from app.tick import tick

    Agents(tcfg, db).research().add_manual(site, "Sai Shiksha Coaching Institute", "Bhubaneswar")
    out = tick(tcfg, db, force=True)
    assert out["pipeline"]["enriched"] == 1 and out["pipeline"]["drafted"] == 1  # all in one tick
    lead = db.find_lead(domain="localhost")
    assert lead["status"] == "drafted", lead["status"]

    _approve_all(db)
    out = tick(tcfg, db, force=True)
    assert out["send"]["sent"] == 1 and len(FakeSMTP.sent) == 1
    assert db.find_lead(domain="localhost")["status"] == "sent"


def test_one_email_per_tick_and_gap_enforced(tcfg, db):
    from app.agents.email_writer import apply_signature
    from app.tick import tick

    body = ("Hi team,\n\nI came across your company while looking at distributors in Cuttack.\n\n"
            "One recent project of ours is collectiqservices.com.\n\nWould a 15-minute call next week be useful?")
    for d in ("a.in", "b.in", "c.in"):
        db.add_lead(company=d, domain=d, email=f"info@{d}", matched_project="collectiq", sender_id="amartya",
                    email_subject="Order tracking", status="approved",
                    email_body=apply_signature(tcfg, body, tcfg.member("amartya")))
    assert tick(tcfg, db, force=True)["send"]["sent"] == 1
    second = tick(tcfg, db, force=True)["send"]
    assert second["sent"] == 0 and second["skipped"] == "waiting for random gap"
    # Pretend the gap has passed.
    db.kv_set("next_send_at", (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="seconds"))
    assert tick(tcfg, db, force=True)["send"]["sent"] == 1
    assert len(FakeSMTP.sent) == 2


def test_ticks_are_spaced(tcfg, db):
    from app.tick import tick

    assert "pipeline" in tick(tcfg, db)
    assert tick(tcfg, db)["skipped"].startswith("too soon")
    assert "pipeline" in tick(tcfg, db, force=True)


def test_stuck_sending_is_failed_not_resent(tcfg, db):
    from app.agents.sender import SenderAgent

    lead_id = db.add_lead(company="Stuck", domain="stuck.in", email="info@stuck.in", status="sending")
    db.execute("UPDATE leads SET updated_at=? WHERE id=?",
               ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"), lead_id))
    assert SenderAgent(tcfg, db).recover_stuck() == 1
    lead = db.get_lead(lead_id)
    assert lead["status"] == "failed" and "Sent folder" in lead["error"]


def test_tick_endpoint(tcfg, db, monkeypatch):
    from app.web import create_app

    monkeypatch.setenv("CRON_SECRET", "s3cret")
    client = create_app(dev=True, cfg=tcfg, db=db).test_client()
    r = client.get("/api/tick")
    assert r.status_code == 200 and "pipeline" in r.get_json()
    assert client.get("/api/tick?force=1").get_json()["skipped"].startswith("too soon")  # no secret: can't force
    r = client.get("/api/tick?force=1", headers={"Authorization": "Bearer s3cret"})
    assert "pipeline" in r.get_json()
    assert "@" not in r.get_data(as_text=True)  # counts only, no lead data

    monkeypatch.setenv("TICK_REQUIRE_SECRET", "true")
    assert client.get("/api/tick").status_code == 401
