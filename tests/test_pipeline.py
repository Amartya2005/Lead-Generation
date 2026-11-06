"""Offline tests: extraction, scoring, matching, email rules, robots.txt, and a
full local pipeline run against fixture sites + a fake SMTP server."""
from __future__ import annotations

import functools
import http.server
import json
import shutil
import smtplib
import threading
from pathlib import Path

import pytest
import yaml

FIX = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- fixtures
@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """Config copy with confirmed portfolio, no LLM, fast crawl, no send window."""
    cdir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cdir)
    port = yaml.safe_load((cdir / "portfolio.yaml").read_text())
    for p in port["projects"]:
        p["confirmed"] = True
    (cdir / "portfolio.yaml").write_text(yaml.safe_dump(port))
    s = yaml.safe_load((cdir / "settings.yaml").read_text())
    s["crawl"].update(per_host_delay_seconds=0, use_playwright_fallback=False, cache_hours=0)
    s["sending"].update(min_delay_seconds=0, max_delay_seconds=0, send_window={})
    s["storage"]["db_path"] = str(tmp_path / "leads.db")
    (cdir / "settings.yaml").write_text(yaml.safe_dump(s))
    team = yaml.safe_load((cdir / "team.yaml").read_text())
    team["members"].append({"id": "priya", "name": "Priya Das", "title": "Business Development",
                            "email": "priya@focustriggerautomation.com", "phone": "", "active": True})
    (cdir / "team.yaml").write_text(yaml.safe_dump(team))

    monkeypatch.setenv("LEADGEN_CONFIG_DIR", str(cdir))
    for k in [k for k in __import__("os").environ if k.startswith(("SMTP_", "IMAP_", "DEFAULT_SENDER"))]:
        monkeypatch.delenv(k)
    for k, v in {"LLM_PROVIDER": "none", "HUMAN_APPROVAL": "true", "DRY_RUN": "false", "DAILY_SEND_LIMIT": "25",
                 "SMTP_USER": "focustriggerautomation@gmail.com", "SMTP_PASSWORD": "app-pass"}.items():
        monkeypatch.setenv(k, v)
    import importlib

    import app.config as config_mod
    importlib.reload(config_mod)
    return config_mod.Config(env_file=tmp_path / "missing.env")


@pytest.fixture()
def db(cfg):
    from app.db import DB
    return DB(cfg.settings["storage"]["db_path"])


def _serve(directory: Path, host: str):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]
    srv = http.server.ThreadingHTTPServer((host, 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def sites():
    a = _serve(FIX / "site_old", "localhost")
    b = _serve(FIX / "site_blocked", "127.0.0.1")
    yield f"http://localhost:{a.server_address[1]}/", f"http://127.0.0.1:{b.server_address[1]}/"
    a.shutdown()
    b.shutdown()


# ------------------------------------------------------------------ units
def test_extract_signals():
    from app.tools.extract import extract

    html = (FIX / "site_old" / "index.html").read_text()
    s = extract(html, "http://example.in/", "example.in")
    assert s["copyright_year"] == 2017
    assert s["has_viewport"] is False
    assert s["jquery_version"] == "1.7"
    assert s["whatsapp_links"] == 1
    assert s["spreadsheet_links"] == 1
    assert "linkedin.com/company/sai-shiksha" in s["linkedin"]
    assert s["pages"]["contact"].endswith("/contact.html")
    assert s["founded"] == 2009
    assert any("whatsapp" in p for p in s["manual_phrases"])


def test_email_cleaning_prefers_role_address_on_own_domain():
    from app.tools.extract import extract

    s = extract((FIX / "site_old" / "contact.html").read_text(), "http://saishiksha.in/", "saishiksha.in")
    assert s["emails"][0] == "info@saishiksha.in"
    assert not any(e.endswith(".png") for e in s["emails"])


def test_geo(cfg):
    from app.tools.geo import Geo

    g = Geo(cfg)
    assert g.detect("Plot 12, Saheed Nagar, Bhubaneswar, Odisha 751007")["tier"] == "odisha"
    assert g.detect("Office in Indore, Madhya Pradesh 452001")["tier"] == "tier2"
    assert g.detect("Andheri East, Mumbai 400069")["tier"] == "metro"
    us = g.detect("Our offices in New York, California and Toronto. Call USA support.")
    assert us["is_india"] is False and us["weight"] == 0


def test_validator_catches_invented_claims(cfg):
    from app.tools.email_rules import validate

    body = ("Hi team,\n\nWe increased leads by 300% for clients like Tata. See bigagency.com for our case study!!\n\n"
            "Interested?\n\nRegards,\nA")
    problems = validate("Hi", body, allowed_domains=cfg.allowed_email_domains, required_domain="collectiqservices.com",
                        allowed_numbers={"15"})
    text = " ".join(problems)
    for expected in ("percentage", "results claim", "non-portfolio domain", "does not reference", "exclamation"):
        assert expected in text, expected


def test_validator_word_limit(cfg):
    from app.tools.email_rules import validate

    body = "Hi,\n\n" + "word " * 130 + "\n\nWould a call work?\n\nRegards,\nA"
    problems = validate("Hi", body + " collectiqservices.com", allowed_domains=cfg.allowed_email_domains,
                        required_domain="collectiqservices.com", allowed_numbers=set())
    assert any("too long" in p for p in problems)


def test_matching_prefers_client_work(cfg):
    from app.agents.matching import best_project

    assert best_project(cfg, ["crm", "automation"], "education")[0]["id"] == "collectiq"
    assert best_project(cfg, ["registration", "website"], "events")[0]["id"] == "startupjathara"
    assert best_project(cfg, ["nonexistent_need"], None)[0] is None


def test_robots_semantics(cfg, db, sites):
    from app.tools.http import PoliteFetcher, RobotsDisallowed

    ok, blocked = sites
    f = PoliteFetcher(cfg, db)
    assert f.allowed(ok)
    assert not f.allowed(ok + "private/x")
    assert not f.allowed(blocked)
    with pytest.raises(RobotsDisallowed):
        f.fetch(blocked)


# ------------------------------------------------------------- end-to-end
class FakeSMTP:
    sent: list = []

    def __init__(self, *a, **k): ...
    def ehlo(self): ...
    def has_extn(self, name): return False
    def login(self, u, p): ...
    def quit(self): ...
    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_full_pipeline(cfg, db, sites, monkeypatch, tmp_path):
    from app.agents.sender import SenderAgent
    from app.graph import Agents, run_pipeline
    from app.review import export_csv, import_csv

    ok, blocked = sites
    a = Agents(cfg, db)
    a.research().add_manual(ok, "Sai Shiksha Coaching Institute", "Bhubaneswar")
    a.research().add_manual(blocked, "Blocked Co", "Cuttack")

    results = run_pipeline(cfg, db, stages=["enrich", "qualify", "match", "write"])
    assert results["enrich"] == {"enriched": 1, "blocked_robots": 1}

    lead = db.find_lead(domain="localhost")
    assert lead["status"] == "drafted", dict(lead)
    assert lead["email"] == "info@saishiksha.in"
    assert lead["tier"] == "odisha" and lead["score"] >= 6
    assert lead["matched_project"] == "collectiq"
    assert json.loads(lead["validation"]) == []
    body = lead["email_body"].split("\nRegards,")[0]
    from app.tools.email_rules import word_count
    assert word_count(body) <= 120
    assert "collectiqservices.com" in lead["email_body"]

    assert lead["sender_id"] == "amartya"
    assert "Amartya\nFocusTrigger Automation Pvt Ltd\nfocustriggerautomation.com" in lead["email_body"]
    assert "At FocusTrigger Automation, we build" in lead["email_body"]

    # Approval mode: nothing is sent before review.
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    assert SenderAgent(cfg, db).run()["sent"] == 0

    # Human approves via CSV round trip.
    path = export_csv(cfg, db, str(tmp_path / "review.csv"))
    text = Path(path).read_text().replace(f"{lead['id']},,", f"{lead['id']},approve,", 1)
    Path(path).write_text(text)
    assert import_csv(cfg, db, path)["approved"] == 1

    out = SenderAgent(cfg, db).run()
    assert out["sent"] == 1 and len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "info@saishiksha.in" and msg["List-Unsubscribe"]
    assert msg["From"] == "Amartya | FocusTrigger Automation <focustriggerautomation@gmail.com>"
    assert db.get_lead(lead["id"])["status"] == "sent"
    assert db.is_suppressed("info@saishiksha.in", None) == "emailed"

    # Re-approving the same lead must not send twice (suppression).
    db.update_lead(lead["id"], status="approved")
    assert SenderAgent(cfg, db).run()["sent"] == 0
    assert db.get_lead(lead["id"])["status"] == "suppressed"


def test_daily_limit_and_unconfirmed_portfolio(cfg, db, monkeypatch):
    from app.agents.sender import SenderAgent

    from app.db import now

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    for i in range(3):  # 3 emails already sent from the shared mailbox today
        db.add_lead(company=f"Old {i}", domain=f"old{i}.in", status="sent", sent_from="shared", sent_at=now())
    _approved_lead(cfg, db, "fresh.in")
    s = SenderAgent(cfg, db)
    s.daily_limit = 3
    out = s.run()
    assert out["sent"] == 0 and out["blocked"] == 1
    assert "daily limit" in db.events_for(db.find_lead(domain="fresh.in")["id"])[-1]["detail"]

    cfg.project("collectiq")["confirmed"] = False
    s.daily_limit = 25
    lead_id = db.add_lead(company="X", website="https://x.in/", domain="x.in", email="a@x.in",
                          matched_project="collectiq", email_subject="s", email_body="b", status="approved")
    from app.agents.sender import SendBlocked
    with pytest.raises(SendBlocked, match="not confirmed"):
        s.check(db.get_lead(lead_id))


def test_llm_draft_rejected_then_template_fallback(cfg, db):
    """An LLM draft that invents results is rejected; the agent falls back to the safe template."""
    from app.agents.email_writer import EmailAgent

    class BadLLM:
        enabled = True
        def json(self, system, prompt):
            return {"subject": "Grow fast", "body": "Hi,\n\nWe boosted revenue 3x for 50 clients. Visit agency.com!!"}

    lead_id = db.add_lead(company="Acme Traders", website="https://acme.in/", domain="acme.in", email="info@acme.in",
                          matched_project="collectiq", needs='["erp"]', industry="distribution", city="Cuttack",
                          observed_problem="", status="matched")
    subject, body, problems, source = EmailAgent(cfg, db, BadLLM()).draft(dict(db.get_lead(lead_id)))
    assert source == "template" and problems == []
    assert "collectiqservices.com" in body and "3x" not in body


def test_llm_draft_accepted_when_clean(cfg, db):
    from app.agents.email_writer import EmailAgent

    class GoodLLM:
        enabled = True
        def json(self, system, prompt):
            return {"subject": "Order tracking at Acme Traders",
                    "body": "Hi Acme Traders team,\n\nI came across Acme Traders while looking at distributors in "
                            "Cuttack.\n\nWe recently built the website, CRM/ERP system and workflow automation for "
                            "CollectIQ Services (collectiqservices.com).\n\nWould a 15-minute call next week be useful?"}

    lead_id = db.add_lead(company="Acme Traders", website="https://acme.in/", domain="acme.in", email="info@acme.in",
                          matched_project="collectiq", needs='["erp"]', status="matched")
    _, body, problems, source = EmailAgent(cfg, db, GoodLLM()).draft(dict(db.get_lead(lead_id)))
    assert source == "llm" and problems == [] and body.rstrip().endswith("won't email again.")


def test_directory_title_parsing_and_queries(cfg, db):
    from app.agents.research import ResearchAgent
    from app.tools.geo import Geo

    r = ResearchAgent(cfg, db, fetcher=None, search=None, geo=Geo(cfg))  # type: ignore[arg-type]
    assert r._directory_name("ABC Interiors in Saheed Nagar, Bhubaneswar - Best Interior Designers - Justdial") == "ABC Interiors"
    assert r._directory_name("Top 10 Coaching Centres in Cuttack - Justdial") is None
    assert r._directory_name("Kalinga Steel Industries - Manufacturer of TMT Bars from Rourkela") == "Kalinga Steel Industries"
    qs = r.build_queries()
    assert len(qs) == cfg.settings["research"]["queries_per_run"] and len(set(qs)) == len(qs)


def test_research_skips_platforms_and_dedupes(cfg, db):
    from app.agents.research import ResearchAgent
    from app.tools.geo import Geo

    class FakeSearch:
        def search(self, q, n):
            return [
                {"title": "Sai Clinic | Home", "url": "https://www.saiclinic.in/about", "snippet": "Clinic in Puri", "engine": "duckduckgo"},
                {"title": "Sai Clinic", "url": "https://saiclinic.in/", "snippet": "", "engine": "duckduckgo"},
                {"title": "Sai Clinic - Facebook", "url": "https://facebook.com/saiclinic", "snippet": "", "engine": "duckduckgo"},
                {"title": "Dept of Health", "url": "https://health.odisha.gov.in/", "snippet": "", "engine": "duckduckgo"},
            ]

    r = ResearchAgent(cfg, db, fetcher=None, search=FakeSearch(), geo=Geo(cfg))  # type: ignore[arg-type]
    assert r.run_search(10) == 1
    lead = db.find_lead(domain="saiclinic.in")
    assert lead["company"] == "Sai Clinic" and lead["website"] == "https://www.saiclinic.in/"


# ------------------------------------------------------------------ team
BODY = ("Hi Fresh team,\n\nI came across Fresh while looking at distributors in Cuttack.\n\n"
        "One recent project of ours is collectiqservices.com.\n\nWould a 15-minute call next week be useful?")


def _approved_lead(cfg, db, domain, sender="amartya", project="collectiq"):
    from app.agents.email_writer import apply_signature

    body = apply_signature(cfg, BODY, cfg.member(sender))
    if project == "amartya":
        body = body.replace("collectiqservices.com", "amartya.focustriggerautomation.com")
    return db.add_lead(company="Fresh", website=f"https://{domain}/", domain=domain, email=f"info@{domain}",
                       matched_project=project, sender_id=sender, email_subject="Order tracking at Fresh",
                       email_body=body, status="approved")


def test_personal_portfolio_only_for_owner(cfg):
    from app.agents.email_writer import check_draft
    from app.agents.matching import best_project

    for needs in (["website"], ["custom_app"], ["website", "automation", "custom_app"]):
        proj, _ = best_project(cfg, needs, None, "priya")
        assert proj and proj["id"] != "amartya"
    lead = {"matched_project": "amartya", "sender_id": "priya", "signals": None, "company": "X"}
    assert "personal portfolio" in check_draft(cfg, lead, "s", "b")[0]


def test_round_robin_assignment(cfg, db):
    from app.agents.matching import MatchingAgent

    cfg.team["assignment"] = "round_robin"
    for i in range(4):
        db.add_lead(company=f"Co {i}", domain=f"co{i}.in", needs='["crm"]', status="qualified")
    MatchingAgent(cfg, db).run()
    assert db.assigned_counts() == {"amartya": 2, "priya": 2}


def test_as_member_override_and_signature(cfg, db):
    from app.agents.email_writer import EmailAgent
    from app.agents.matching import MatchingAgent

    lead_id = db.add_lead(company="Acme Traders", domain="acme.in", email="info@acme.in", needs='["erp"]',
                          status="qualified")
    MatchingAgent(cfg, db, sender_override="priya").run()
    EmailAgent(cfg, db, llm=type("L", (), {"enabled": False})()).run()
    lead = db.get_lead(lead_id)
    assert lead["sender_id"] == "priya" and lead["validation"] == "[]"
    assert "Priya Das\nBusiness Development | FocusTrigger Automation Pvt Ltd" in lead["email_body"]
    with pytest.raises(ValueError):
        MatchingAgent(cfg, db, sender_override="nobody")


def test_member_own_mailbox_and_per_mailbox_limit(cfg, db, monkeypatch):
    from app.agents.sender import SenderAgent
    from app.db import now

    monkeypatch.setenv("SMTP_PASSWORD_PRIYA", "priya-app-pass")
    mb = cfg.mailbox(cfg.member("priya"))
    assert mb["id"] == "member:priya" and mb["from_email"] == "priya@focustriggerautomation.com"
    assert cfg.mailbox(cfg.member("amartya"))["id"] == "shared"

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent = []
    for i in range(2):  # shared mailbox is at its limit...
        db.add_lead(company=f"Old {i}", domain=f"old{i}.in", status="sent", sent_from="shared", sent_at=now())
    _approved_lead(cfg, db, "a.in", "amartya")
    _approved_lead(cfg, db, "p.in", "priya")
    s = SenderAgent(cfg, db)
    s.daily_limit = 2
    out = s.run()
    # ...so only Priya's email goes out, from her own mailbox.
    assert out["sent"] == 1 and out["blocked"] == 1
    assert FakeSMTP.sent[0]["From"] == "Priya Das | FocusTrigger Automation <priya@focustriggerautomation.com>"
    assert db.find_lead(domain="p.in")["sent_from"] == "member:priya"


def test_assign_resigns_and_requires_reapproval(cfg, db, monkeypatch):
    import main

    lead_id = _approved_lead(cfg, db, "z.in", "amartya")
    monkeypatch.setattr(main, "_ctx", lambda args: (cfg, db))
    main.main(["assign", "priya", str(lead_id)])
    lead = db.get_lead(lead_id)
    assert lead["sender_id"] == "priya" and lead["status"] == "drafted"
    assert "Priya Das" in lead["email_body"] and "\nAmartya\n" not in lead["email_body"]


def test_assign_rematches_personal_portfolio(cfg, db, monkeypatch):
    import main

    lead_id = _approved_lead(cfg, db, "y.in", "amartya", project="amartya")
    db.update_lead(lead_id, needs='["website"]', status="drafted")
    monkeypatch.setattr(main, "_ctx", lambda args: (cfg, db))
    main.main(["assign", "priya", str(lead_id)])
    lead = db.get_lead(lead_id)
    assert lead["sender_id"] == "priya" and lead["matched_project"] != "amartya" and lead["status"] == "drafted"


def test_llm_falls_back_to_next_model_on_503(cfg, monkeypatch):
    import requests
    from app.llm import LLM

    cfg.settings["llm"].update(provider="gemini", requests_per_minute=10000)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    calls = []

    class Resp:
        def __init__(self, code, payload):
            self.status_code, self._p, self.text = code, payload, str(payload)
        def json(self):
            return self._p
        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(response=self)

    def fake_post(url, **kw):
        calls.append(url.split("/models/")[1].split(":")[0])
        if len(calls) == 1:
            return Resp(503, {"error": "busy"})
        return Resp(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]})

    monkeypatch.setattr(requests, "post", fake_post)
    llm = LLM(cfg)
    assert llm.json("s", "p") == {"ok": True}
    assert calls[0] == "gemini-3.8-flash" and calls[1] == "gemini-flash-latest"


def test_db_migrates_old_schema(tmp_path):
    import sqlite3
    from app.db import DB

    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, company TEXT NOT NULL, domain TEXT UNIQUE, "
              "status TEXT NOT NULL DEFAULT 'discovered', score INTEGER, sent_at TEXT, created_at TEXT, updated_at TEXT)")
    c.commit()
    c.close()
    db = DB(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(leads)")}
    assert {"sender_id", "sent_from"} <= cols
