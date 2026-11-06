"""Web review page: auth, CSRF, lockout, permissions, approve/edit/reassign, config layering, locks."""
from __future__ import annotations

import re

import pytest
from werkzeug.security import generate_password_hash

PW = "correct horse battery"
BODY = ("Hi Fresh team,\n\nI came across Fresh while looking at distributors in Cuttack.\n\n"
        "One recent project of ours is collectiqservices.com.\n\nWould a 15-minute call next week be useful?")


@pytest.fixture()
def client(cfg, db):
    from app.web import create_app

    db.upsert_user("amartya", generate_password_hash(PW), is_admin=True)
    db.upsert_user("priya", generate_password_hash(PW), is_admin=False)
    app = create_app(dev=True, cfg=cfg, db=db)
    app.config["TESTING"] = True
    return app.test_client()


def _csrf(client, path="/login") -> str:
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _login(client, user="amartya", pw=PW):
    return client.post("/login", data={"username": user, "password": pw, "csrf": _csrf(client)})


def _draft(cfg, db, domain="fresh.in", sender="amartya", body=BODY, status="drafted"):
    from app.agents.email_writer import apply_signature

    return db.add_lead(company="Fresh Traders", website=f"https://{domain}/", domain=domain, email=f"info@{domain}",
                       matched_project="collectiq", sender_id=sender, score=8, needs='["erp"]',
                       email_subject="Order tracking at Fresh", email_body=apply_signature(cfg, body, cfg.member(sender)),
                       validation="[]", status=status)


def test_requires_login_and_healthz_is_public(client):
    assert client.get("/healthz").data == b"ok"
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_login_logout_and_security_headers(client):
    r = _login(client)
    assert r.status_code == 302
    page = client.get("/")
    assert page.status_code == 200 and "Hi Amartya" in page.get_data(as_text=True)
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Frame-Options"] == "DENY"
    client.post("/logout", data={"csrf": _csrf(client, "/")})
    assert client.get("/").status_code == 302


def test_wrong_password_and_lockout(client, cfg):
    for _ in range(cfg.settings["web"]["login_max_failures"]):
        assert _login(client, pw="nope").status_code == 401
    assert _login(client).status_code == 429  # locked even with the right password


def test_post_without_csrf_is_rejected(client, cfg, db):
    _login(client)
    lead_id = _draft(cfg, db)
    assert client.post(f"/leads/{lead_id}/approve", data={}).status_code == 400
    assert db.get_lead(lead_id)["status"] == "drafted"


def test_open_redirect_blocked(client):
    r = client.post("/login?next=//evil.com", data={"username": "amartya", "password": PW, "csrf": _csrf(client)})
    assert r.headers["Location"] in ("/", "http://localhost/")


def test_approve_flow_and_next_draft(client, cfg, db):
    _login(client)
    a, b = _draft(cfg, db, "a.in"), _draft(cfg, db, "b.in")
    page = client.get(f"/leads/{a}").get_data(as_text=True)
    assert "Approve" in page and "collectiqservices.com" in page
    lead = db.get_lead(a)
    r = client.post(f"/leads/{a}/approve", data={"csrf": _csrf(client, "/"), "subject": lead["email_subject"],
                                                 "body": lead["email_body"]})
    assert db.get_lead(a)["status"] == "approved"
    assert r.headers["Location"].endswith(f"/leads/{b}")  # jumps to the next draft
    assert any(e["agent"] == "web:amartya" and e["event"] == "approved" for e in db.events_for(a))


def test_edit_that_breaks_rules_cannot_be_approved(client, cfg, db):
    _login(client)
    lead_id = _draft(cfg, db)
    lead = db.get_lead(lead_id)
    bad = lead["email_body"].replace("Would a 15-minute", "We boosted sales 300% for clients like Tata. Would a 15-minute")
    client.post(f"/leads/{lead_id}/approve", data={"csrf": _csrf(client, "/"), "subject": lead["email_subject"], "body": bad})
    lead = db.get_lead(lead_id)
    assert lead["status"] == "drafted" and "percentage" in lead["validation"]
    assert "300%" in lead["email_body"]  # the edit is kept so the reviewer can fix it


def test_editing_an_approved_draft_needs_reapproval(client, cfg, db):
    _login(client)
    lead_id = _draft(cfg, db, status="approved")
    lead = db.get_lead(lead_id)
    client.post(f"/leads/{lead_id}/save", data={"csrf": _csrf(client, "/"), "subject": "New subject here",
                                                "body": lead["email_body"]})
    lead = db.get_lead(lead_id)
    assert lead["status"] == "drafted" and lead["email_subject"] == "New subject here"


def test_member_cannot_touch_others_leads(client, cfg, db):
    _login(client, "priya")
    theirs = _draft(cfg, db, "x.in", sender="amartya")
    mine = _draft(cfg, db, "y.in", sender="priya")
    token = _csrf(client, "/")
    assert client.post(f"/leads/{theirs}/reject", data={"csrf": token}).status_code == 403
    assert db.get_lead(theirs)["status"] == "drafted"
    client.post(f"/leads/{mine}/reject", data={"csrf": token})
    assert db.get_lead(mine)["status"] == "rejected"
    assert "Fresh Traders" in client.get("/leads?status=drafted&who=all").get_data(as_text=True)


def test_reassign_swaps_signature(client, cfg, db):
    _login(client)
    lead_id = _draft(cfg, db, status="approved")
    client.post(f"/leads/{lead_id}/assign", data={"csrf": _csrf(client, "/"), "member": "priya"})
    lead = db.get_lead(lead_id)
    assert lead["sender_id"] == "priya" and lead["status"] == "drafted" and "Priya Das" in lead["email_body"]


def test_suppression_from_web_blocks_approval(client, cfg, db):
    _login(client)
    lead_id = _draft(cfg, db)
    client.post("/suppression", data={"csrf": _csrf(client, "/"), "value": "fresh.in", "reason": "unsubscribed"})
    assert db.is_suppressed(None, "fresh.in") == "unsubscribed"
    lead = db.get_lead(lead_id)
    client.post(f"/leads/{lead_id}/approve", data={"csrf": _csrf(client, "/"), "subject": lead["email_subject"],
                                                   "body": lead["email_body"]})
    assert db.get_lead(lead_id)["status"] == "suppressed"


def test_user_removed_from_team_loses_access(client, cfg, db):
    _login(client, "priya")
    cfg.team["members"] = [m for m in cfg.members if m["id"] != "priya"]
    assert client.get("/").status_code == 302


def test_pages_render(client, cfg, db):
    _login(client)
    _draft(cfg, db)
    for path in ("/", "/leads?status=drafted", "/leads?status=sent&who=all&q=fresh", "/suppression", "/team"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "not confirmed" not in client.get("/team").get_data(as_text=True)  # fixture confirms all
    assert client.get("/leads/99999").status_code == 404


# ------------------------------------------------------------ config + locks
def test_local_config_overrides_merge(tmp_path, monkeypatch, cfg):
    from app.config import Config

    local = tmp_path / "local"
    local.mkdir()
    (local / "settings.yaml").write_text("sending:\n  daily_limit: 7\n")
    (local / "team.yaml").write_text("members:\n  - {id: neha, name: Neha}\n  - {id: amartya, name: Amartya}\n"
                                     "default_member: neha\n")
    monkeypatch.setenv("LEADGEN_LOCAL_CONFIG_DIR", str(local))
    monkeypatch.delenv("DAILY_SEND_LIMIT")
    c = Config(env_file=tmp_path / "none.env")
    assert c.settings["sending"]["daily_limit"] == 7
    assert c.settings["sending"]["min_delay_seconds"] == 0       # untouched keys keep base values
    assert [m["id"] for m in c.members] == ["neha", "amartya"]  # lists replace
    assert c.member(None)["id"] == "neha"
    assert c.company["legal_name"] == "FocusTrigger Automation Pvt Ltd"


def test_portfolio_owner_must_exist(tmp_path, monkeypatch, cfg):
    from app.config import Config

    local = tmp_path / "local"
    local.mkdir()
    (local / "team.yaml").write_text("members:\n  - {id: neha, name: Neha}\n")
    monkeypatch.setenv("LEADGEN_LOCAL_CONFIG_DIR", str(local))
    with pytest.raises(ValueError, match="owner 'amartya'"):
        Config(env_file=tmp_path / "none.env")


def test_sender_lock_prevents_parallel_runs(cfg, db):
    from app.agents.sender import SenderAgent
    from app.tools.locks import exclusive

    lock = cfg.path(cfg.settings["storage"]["db_path"]).with_suffix(".send.lock")
    with exclusive(lock):
        assert SenderAgent(cfg, db).run(ignore_window=True)["skipped"] == "already running"


def test_backup(cfg, db, tmp_path):
    import sqlite3

    db.add_lead(company="Keep Me", domain="keep.in")
    dest = db.backup(tmp_path / "bk")
    assert sqlite3.connect(dest).execute("SELECT company FROM leads").fetchone()[0] == "Keep Me"


def test_web_user_cli(cfg, db, monkeypatch):
    import main

    monkeypatch.setattr(main, "_ctx", lambda args: (cfg, db))
    main.main(["web-user", "add", "priya", "--password", "long-enough-pw"])
    assert db.get_user("priya") and not db.get_user("priya")["is_admin"]
    with pytest.raises(SystemExit):
        main.main(["web-user", "add", "stranger", "--password", "long-enough-pw"])
    with pytest.raises(SystemExit):
        main.main(["web-user", "add", "priya", "--password", "short"])
