"""Lead-generation-only mode: sending hard-disabled, confirmed-only portfolio, stricter lead
quality filters and the 'ready for review' status."""
from __future__ import annotations

import functools
import http.server
import json
import smtplib
import threading
from pathlib import Path

import pytest

from tests.test_web import _csrf, _draft, _login, client  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parent.parent


class ExplodingSMTP:
    """Any attempt to open an SMTP connection fails the test."""

    def __init__(self, *a, **k):
        raise AssertionError("SMTP must never be opened in lead-generation-only mode")


@pytest.fixture()
def off(cfg, monkeypatch):
    """The test config with sending switched OFF, as in the repo default."""
    cfg.settings["sending"]["enabled"] = False
    cfg.settings["research"]["queries_per_run"] = 0
    monkeypatch.setattr(smtplib, "SMTP", ExplodingSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", ExplodingSMTP)
    return cfg


def _approved(cfg, db, domain="fresh.in"):
    lead_id = _draft(cfg, db, domain)
    db.update_lead(lead_id, status="approved")
    return lead_id


def _research(cfg, db, search=None):
    from app.agents.research import ResearchAgent
    from app.tools.geo import Geo

    return ResearchAgent(cfg, db, fetcher=None, search=search, geo=Geo(cfg))  # type: ignore[arg-type]


# ------------------------------------------------------------------ sending is off
def test_repo_default_is_lead_generation_only(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADGEN_CONFIG_DIR", str(ROOT / "config"))
    monkeypatch.setenv("DRY_RUN", "false")  # an env var must not be able to switch sending back on
    monkeypatch.setenv("SENDING_ENABLED", "true")
    from app.config import Config

    c = Config(env_file=tmp_path / "missing.env")
    assert c.sending_enabled is False and c.settings["sending"]["dry_run"] is True
    assert "send" not in __import__("app.graph", fromlist=["STAGES"]).STAGES


def test_sender_never_opens_smtp_when_disabled(off, db):
    from app.agents.sender import SendBlocked, SenderAgent

    lead_id = _approved(off, db)
    s = SenderAgent(off, db)
    assert "sending disabled" in s.run(ignore_window=True)["skipped"]
    assert "sending disabled" in s.send_one(ignore_window=True)["skipped"]
    with pytest.raises(SendBlocked):
        s.deliver(object(), None)  # type: ignore[arg-type]
    with pytest.raises(SendBlocked):
        s._smtp_for(off.mailbox(off.member(None)))
    assert db.get_lead(lead_id)["status"] == "approved"
    assert db.scalar("SELECT count(*) FROM events WHERE agent='sender'") == 0


def test_graph_has_no_send_node_and_run_send_is_ignored(off, db):
    from app.graph import Agents, build_nodes, run_pipeline

    a = Agents(off, db)
    assert "send" not in build_nodes(a)
    a.close()
    _approved(off, db)
    results = run_pipeline(off, db, stages=["write"], send=True)
    assert "send" not in results


def test_tick_drafts_but_never_sends_or_syncs_inbox(off, db):
    from app.tick import tick

    _approved(off, db)
    out = tick(off, db, force=True)
    assert out["send"] == {"skipped": "sending disabled"} and "inbox" not in out
    assert "pipeline" in out


def test_web_approve_works_but_nothing_is_sent(off, db, client):  # noqa: F811
    lead_id = _draft(off, db)
    _login(client)
    assert "Lead-generation-only mode" in client.get("/").get_data(as_text=True)
    page = client.get(f"/leads/{lead_id}").get_data(as_text=True)
    assert "ready for review" in page
    r = client.post(f"/leads/{lead_id}/approve", data={"csrf": _csrf(client, f"/leads/{lead_id}")},
                    follow_redirects=True)
    assert "Sending is disabled" in r.get_data(as_text=True)
    assert db.get_lead(lead_id)["status"] == "approved"
    r = client.post("/admin/test-email", data={"csrf": _csrf(client, "/team"), "to": "me@example.com"},
                    follow_redirects=True)
    assert "Not sent" in r.get_data(as_text=True)
    assert "Send test email" not in client.get("/team").get_data(as_text=True)


# ------------------------------------------------------------------ portfolio
def test_matching_uses_only_confirmed_projects(cfg, db):
    from app.agents.matching import MatchingAgent

    for p in cfg.portfolio:
        p["confirmed"] = False
    lead_id = db.add_lead(company="Acme Traders", website="https://acme.in/", domain="acme.in", needs='["erp"]',
                          status="qualified")
    assert MatchingAgent(cfg, db).run()["awaiting_portfolio"] == 1
    lead = db.get_lead(lead_id)
    assert lead["status"] == "qualified" and lead["matched_project"] is None
    assert lead["match_reason"].startswith("waiting for portfolio confirmation")
    from app.tools.report import review_status
    assert review_status(cfg, lead) == "awaiting_portfolio"

    cfg.project("collectiq")["confirmed"] = True  # a human confirmed it
    assert MatchingAgent(cfg, db).run()["matched"] == 1
    assert db.get_lead(lead_id)["matched_project"] == "collectiq"


def test_email_writer_never_cites_unconfirmed_project(cfg, db):
    from app.agents.email_writer import EmailAgent, check_draft
    from app.llm import LLM

    lead_id = _draft(cfg, db)
    cfg.project("collectiq")["confirmed"] = False
    lead = dict(db.get_lead(lead_id))
    assert any("not confirmed" in p for p in check_draft(cfg, lead, lead["email_subject"], lead["email_body"]))
    from app.tools.report import review_status
    assert review_status(cfg, db.get_lead(lead_id)) == "needs_fix"

    db.update_lead(lead_id, status="matched")
    status, _ = EmailAgent(cfg, db, LLM(cfg)).draft_one(db.get_lead(lead_id))
    assert status == "requeued"
    lead = db.get_lead(lead_id)
    assert lead["status"] == "qualified" and lead["matched_project"] is None


def test_confirmed_project_needs_short_claim_free_description(tmp_path, monkeypatch, cfg):
    import yaml

    from app.config import Config

    cdir = Path(__import__("os").environ["LEADGEN_CONFIG_DIR"])
    port = yaml.safe_load((cdir / "portfolio.yaml").read_text())
    port["projects"][0]["description"] = "the award-winning website that increased bookings by 40%"
    port["projects"][1]["description"] = "x" * 130
    port["projects"][2]["confirmed"] = "yes"  # only a real boolean true counts
    (cdir / "portfolio.yaml").write_text(yaml.safe_dump(port))
    c = Config(env_file=tmp_path / "missing.env")
    p0, p1, p2 = c.portfolio[:3]
    assert not p0["confirmed"] and any("number" in d for d in p0["description_problems"])
    assert not p1["confirmed"] and "characters" in p1["description_problems"][0]
    assert not p2["confirmed"]


# ------------------------------------------------------------------ lead quality
@pytest.mark.parametrize("title,url", [
    ("Top 10 Dental Clinics in Puri", "https://localguide.in/puri-dental"),
    ("10 Best Coaching Institutes in Cuttack (2026 Updated)", "https://edublog.in/x"),
    ("Dental Clinics in Puri - LocalGuide", "https://localguide.in/"),
    ("Wedding Planners in Cuttack | Book Now", "https://venues.example.in/"),
    ("Banquet Halls in Bhubaneswar", "https://halls.example.in/"),
    ("Interior inspiration", "https://blog.example.in/best-interior-designers-in-bhubaneswar/"),
    ("Clinics", "https://abc.in/category/clinics/"),
    ("Diagnostic Centres Near Me in Rourkela", "https://example.in/"),
    ("Best Dental Clinic in Puri | Sai Dental Care", "https://someblog.in/sai"),  # tagline + not their domain
    ("Interior Designers Phone Numbers, Bhubaneswar", "https://example.in/"),
])
def test_listing_and_category_results_rejected(cfg, db, title, url):
    r = _research(cfg, db)
    assert r.listing_reason(title, url) or r._site_name(title, url) is None


@pytest.mark.parametrize("title,url,name", [
    ("Sai Dental Care | Best Dental Clinic in Puri", "https://saidentalcare.in/", "Sai Dental Care"),
    ("Best Dental Clinic in Puri | Sai Dental Care", "https://www.saidentalcare.in/", "Sai Dental Care"),
    ("Contact Us - Kalinga Rice Mill", "https://kalingaricemill.com/contact", "Kalinga Rice Mill"),
    ("Home | Utkal Steel Fabricators Pvt Ltd", "https://utkalsteel.in/", "Utkal Steel Fabricators Pvt Ltd"),
    ("Sagar Electronics | Sambalpur", "https://sagarelectronics.in/", "Sagar Electronics"),  # city-named business
])
def test_company_own_sites_kept(cfg, db, title, url, name):
    r = _research(cfg, db)
    assert r.listing_reason(title, url) is None and r._site_name(title, url) == name


def test_directory_category_titles_rejected(cfg, db):
    r = _research(cfg, db)
    for t in ("Interior Designers in Bhubaneswar - Justdial", "Wedding Venues in Cuttack - Justdial",
              "Top Rice Mills in Sambalpur - IndiaMART", "Dental Clinics in Puri - Best Dentists - Justdial"):
        assert r._directory_name(t) is None, t


def test_queries_exclude_directories_and_resolve_requires_own_domain(cfg, db):
    seen = []

    class FakeSearch:
        def search(self, q, n):
            seen.append(q)
            if "official website" in q:
                return [  # a blog mentioning the name, then the real site
                    {"title": "Sai Dental Care Puri reviews", "url": "https://puriguide.in/sai-dental", "snippet": "", "engine": "d"},
                    {"title": "Sai Dental Care", "url": "https://saidentalcare.in/", "snippet": "", "engine": "d"},
                ]
            return [{"title": "Sai Dental Care in Grand Road, Puri - Justdial",
                     "url": "https://www.justdial.com/Puri/sai", "snippet": "", "engine": "d"}]

    r = _research(cfg, db, FakeSearch())
    assert r.run_search(5, ["dental clinic Puri \"contact us\""]) == 1
    assert "-site:justdial.com" in seen[0] and "-site:indiamart.com" in seen[0]
    assert db.find_lead(domain="saidentalcare.in")["company"] == "Sai Dental Care"
    assert db.find_lead(domain="puriguide.in") is None


def test_queries_are_india_tier2_3_first_and_skip_directories(cfg, db):
    cfg.settings["research"]["queries_per_run"] = 30
    icp = cfg.icp
    metro = set(icp["cities"]["metro"])
    for q in _research(cfg, db).build_queries():
        assert "site:" not in q and not any(f" {m} " in f" {q} " or f'"{m}"' in q for m in metro), q
    assert icp["city_mix"]["odisha"] >= max(v for k, v in icp["city_mix"].items() if k != "odisha")


@pytest.mark.parametrize("html,expect", [
    ("<title>example.in</title><p>This domain is for sale. Make an offer on this domain.</p>", "parked"),
    ("<title>ABC</title><h1>Coming soon</h1><p>Our new website is under construction.</p>", "placeholder"),
    ("<html><body></body></html>", "empty"),
    ("<title>Wedding vendors</title><p>Browse vendors. Add your business. Wedding venues in Cuttack.</p>", "directory"),
])
def test_site_problem_detects_non_company_pages(html, expect):
    from app.tools.extract import extract, site_problem

    assert expect in (site_problem(extract(html, "https://example.in/", "example.in")) or "")


def test_real_company_page_passes_site_check():
    from app.tools.extract import extract, site_problem

    html = (Path(__file__).parent / "fixtures" / "site_old" / "index.html").read_text()
    assert site_problem(extract(html, "https://saishiksha.in/", "saishiksha.in")) is None


def test_enrichment_discards_parked_site(cfg, db, tmp_path):
    from app.graph import Agents

    (tmp_path / "parked").mkdir()
    (tmp_path / "parked" / "index.html").write_text("<title>x</title>This domain is for sale. Buy this domain today.")
    (tmp_path / "parked" / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path / "parked"))
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]
    srv = http.server.ThreadingHTTPServer(("localhost", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        a = Agents(cfg, db)
        lead_id = a.research().add_manual(f"http://localhost:{srv.server_address[1]}/", "Parked Co", "Cuttack")
        assert a.enrichment().run() == {"not_company_site": 1}
        a.close()
    finally:
        srv.shutdown()
    lead = db.get_lead(lead_id)
    assert lead["status"] == "discarded" and "not a real company website" in lead["reasons"]


def test_llm_can_veto_a_listing_site(cfg, db):
    from app.agents.qualification import QualificationAgent

    class ListingLLM:
        enabled = True
        def json(self, system, prompt):
            return {"is_company_site": False, "score": 9, "needs": ["website"], "reason": "directory"}

    sig = {"geo": {"is_india": True, "weight": 3, "location": "Cuttack, Odisha", "tier": "odisha"},
           "copyright_year": 2015, "has_viewport": False, "https": False, "word_count": 50}
    lead_id = db.add_lead(company="City Guide", website="https://cityguide.in/", domain="cityguide.in",
                          email="info@cityguide.in", signals=json.dumps(sig), status="enriched")
    assert QualificationAgent(cfg, db, ListingLLM()).run() == {"discarded": 1}
    assert "not a single company's own website" in db.get_lead(lead_id)["reasons"]


# ------------------------------------------------------------------ output
def test_report_has_ready_for_review_status(cfg, db):
    from app.tools.report import COLUMNS, READY, rows

    ok = _draft(cfg, db, "fresh.in")
    bad = _draft(cfg, db, "stale.in")
    db.update_lead(bad, validation='["too long: 130 words (max 120)"]')
    by_site = {r[1]: r for r in rows(cfg, db)}
    i = COLUMNS.index("Review Status")
    assert COLUMNS[:7] == ["Company", "Website", "Location", "Score", "Matched Project", "Email Draft", "Status"]
    assert by_site["https://fresh.in/"][i] == READY and by_site["https://stale.in/"][i] == "needs_fix"
    assert ok
