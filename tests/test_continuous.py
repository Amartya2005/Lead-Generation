"""Continuous, high-volume discovery: no daily lead/query ceiling, per-tick budgets, provider
back-off, backlog backpressure with automatic resume, parallel enrichment without duplicate
processing, per-source analytics, and unchanged quality filters / email limits. Offline:
DuckDuckGo and the web are faked."""
from __future__ import annotations

import hashlib
import smtplib
import threading
import time
from pathlib import Path

import pytest
import yaml

from app.tools.http import Page
from app.tools.search import SearchRateLimited

ROOT = Path(__file__).resolve().parent.parent
SITE = (Path(__file__).parent / "fixtures" / "site_old" / "index.html").read_text()
CONTACT = (Path(__file__).parent / "fixtures" / "site_old" / "contact.html").read_text()


class ExplodingSMTP:
    def __init__(self, *a, **k):
        raise AssertionError("no email may be sent")


class FakeDDG:
    """Per query: 3 distinct companies, 1 company seen by every query, 1 listicle, 1 directory category."""
    calls: list[str] = []
    rate_limited = False

    def __init__(self, cfg=None):
        self.rate_limit_wait = 60

    @staticmethod
    def _word(seed: str) -> str:
        return "".join(chr(97 + int(c, 16) % 26) for c in hashlib.md5(seed.encode()).hexdigest()[:8]).capitalize()

    def search(self, q, n):
        FakeDDG.calls.append(q)
        if FakeDDG.rate_limited:
            raise SearchRateLimited("202 Ratelimit")
        out = []
        for i in range(3):
            w = self._word(f"{q}|{i}")
            out.append({"title": f"{w} Traders | Home", "url": f"https://www.{w.lower()}traders.in/",
                        "snippet": "Wholesale traders", "engine": "duckduckgo"})
        out += [
            {"title": "Kalpataru Exports | Home", "url": "https://kalpataruexports.in/", "snippet": "", "engine": "duckduckgo"},
            {"title": "Top 10 Traders in Cuttack (2026)", "url": "https://cityblog.in/top-10-traders-in-cuttack/",
             "snippet": "", "engine": "duckduckgo"},
            {"title": "Traders in Cuttack - Justdial", "url": "https://www.justdial.com/Cuttack/Traders",
             "snippet": "", "engine": "duckduckgo"},
        ]
        return out


class FakeWeb:
    """Every company site serves the fixture site (home + contact page); counts fetches and peak concurrency."""
    lock = threading.Lock()
    fetched: list[str] = []
    active = peak = 0
    delay = 0.0

    def __init__(self, cfg=None, db=None):
        pass

    def fetch(self, url, render=None, **kw):
        with FakeWeb.lock:
            FakeWeb.active += 1
            FakeWeb.peak = max(FakeWeb.peak, FakeWeb.active)
            FakeWeb.fetched.append(url)
        try:
            time.sleep(FakeWeb.delay)
            return Page(url, url, 200, CONTACT if url.endswith("contact.html") else SITE, "text/html")
        finally:
            with FakeWeb.lock:
                FakeWeb.active -= 1

    def close(self):
        pass


@pytest.fixture()
def ccfg(cfg, monkeypatch):
    """Repo-like continuous config: sending OFF, old per-run 'caps' set to 1 to prove ticks ignore them."""
    import app.graph

    cfg.settings["sending"]["enabled"] = False
    cfg.settings["research"].update(max_new_leads_per_run=1, queries_per_run=1, seconds_between_queries=[0, 0])
    cfg.settings["tick"]["discovery"]["queries_per_tick"] = 3
    monkeypatch.setattr(smtplib, "SMTP", ExplodingSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", ExplodingSMTP)
    monkeypatch.setattr(app.graph, "SearchClient", FakeDDG)
    monkeypatch.setattr(app.graph, "PoliteFetcher", FakeWeb)
    FakeDDG.calls, FakeDDG.rate_limited = [], False
    FakeWeb.fetched, FakeWeb.active, FakeWeb.peak, FakeWeb.delay = [], 0, 0, 0.0
    return cfg


def _tick(cfg, db):
    from app.tick import tick

    return tick(cfg, db, force=True)


def _leads(db, source="duckduckgo"):
    return db.scalar("SELECT count(*) FROM leads WHERE source=?", (source,))


# ------------------------------------------------------------------ volume
def test_simulated_day_collects_far_more_than_25_unique_leads(ccfg, db):
    per_tick = []
    for _ in range(8):  # eight scheduled ticks in one day
        before = _leads(db)
        out = _tick(ccfg, db)
        assert out["discovery"] == "ran" and out["send"] == {"skipped": "sending disabled"}
        per_tick.append(_leads(db) - before)

    total = _leads(db)
    assert total > 25 and total == 8 * 3 * 3 + 1  # 8 ticks x 3 queries x 3 new companies, plus the shared one once
    assert all(n > 0 for n in per_tick)  # discovery continued on every tick
    assert len(FakeDDG.calls) == 24 > 8  # far past the old 8-queries-a-day ceiling
    assert db.scalar("SELECT count(DISTINCT domain) FROM leads") == db.scalar("SELECT count(*) FROM leads")
    # quality filters held: no listicle, directory or category page became a lead
    assert not db.query("SELECT 1 FROM leads WHERE domain IN ('cityblog.in', 'justdial.com')")
    # the leads flowed through the unchanged pipeline
    assert db.scalar("SELECT count(*) FROM leads WHERE status='sent'") == 0
    assert db.scalar("SELECT count(*) FROM leads WHERE status='drafted'") > 25  # ready for human review


def test_ticks_ignore_old_daily_ceilings_and_keep_going(ccfg, db):
    for _ in range(4):
        _tick(ccfg, db)
    assert _leads(db) > ccfg.settings["research"]["max_new_leads_per_run"] * 10
    before = _leads(db)
    _tick(ccfg, db)  # still discovering after dozens of leads today
    assert _leads(db) > before


def test_per_tick_lead_budget_is_the_only_volume_limit(ccfg, db):
    ccfg.settings["tick"]["discovery"]["max_new_leads_per_tick"] = 4
    _tick(ccfg, db)
    assert _leads(db) == 4
    _tick(ccfg, db)
    assert _leads(db) == 8


# ------------------------------------------------------------------ provider back-off
def test_search_rate_limit_backs_off_across_ticks_then_resumes(ccfg, db):
    from app.tools.backoff import cooling_down

    FakeDDG.rate_limited = True
    out = _tick(ccfg, db)
    assert len(FakeDDG.calls) == 1 and out["discovery"] == "ran"  # no in-tick sleep: one call, then back off
    assert cooling_down(db, "search:backoff")
    _tick(ccfg, db)
    assert len(FakeDDG.calls) == 1  # still backing off: search skipped, nothing slept

    FakeDDG.rate_limited = False
    db.kv_set("search:backoff", '{"until": "2000-01-01T00:00:00+00:00", "minutes": 15}')  # pause expired
    _tick(ccfg, db)
    assert len(FakeDDG.calls) == 4 and _leads(db) > 0
    assert cooling_down(db, "search:backoff") is None


def test_back_off_doubles_up_to_cap(db):
    import json

    from app.tools.backoff import back_off, reset

    assert [back_off(db, "k", "x", first=15, cap=60) for _ in range(4)] == [15, 30, 60, 60]
    reset(db, "k")
    assert back_off(db, "k", "x", first=15, cap=60) == 15
    assert json.loads(db.kv_get("k"))["minutes"] == 15


# ------------------------------------------------------------------ backpressure
def test_backlog_pauses_discovery_drains_then_resumes(ccfg, db):
    ccfg.settings["tick"]["backpressure"].update(pause_discovery_at=10, resume_discovery_below=5)
    for i in range(12):
        db.add_lead(company=f"Backlog {i}", website=f"https://backlog{i}.in/", domain=f"backlog{i}.in",
                    source="manual", city_hint="Bhubaneswar")
    out = _tick(ccfg, db)
    assert out["discovery"].startswith("paused") and FakeDDG.calls == []  # discovery paused...
    assert out["pipeline"]["enriched"] == 12  # ...while the backlog is drained
    assert db.scalar("SELECT count(*) FROM leads WHERE status IN ('discovered','enriched')") == 0

    out = _tick(ccfg, db)
    assert out["discovery"] == "ran" and len(FakeDDG.calls) == 3  # resumed automatically


def test_backpressure_hysteresis(cfg, db):
    from app.tick import _discovery_gate

    t = {"backpressure": {"pause_discovery_at": 10, "resume_discovery_below": 5}}
    ids = [db.add_lead(company=f"L{i}", website=f"https://l{i}.in/", domain=f"l{i}.in") for i in range(12)]
    assert _discovery_gate(db, t)  # 12 >= 10: paused
    for i in ids[:5]:
        db.update_lead(i, status="discarded")
    assert _discovery_gate(db, t)  # 7: still paused (above resume threshold)
    for i in ids[5:8]:
        db.update_lead(i, status="discarded")
    assert _discovery_gate(db, t) is None  # 4 < 5: resumed
    db.add_lead(company="L99", website="https://l99.in/", domain="l99.in")
    db.add_lead(company="L98", website="https://l98.in/", domain="l98.in")
    assert _discovery_gate(db, t) is None  # 6: keeps running until it reaches 10 again


# ------------------------------------------------------------------ concurrency / overlap
def test_parallel_enrichment_processes_each_lead_exactly_once(ccfg, db):
    from app.tick import _enrich

    FakeWeb.delay = 0.05
    for i in range(12):
        db.add_lead(company=f"Par {i}", website=f"https://par{i}.in/", domain=f"par{i}.in", source="manual")
    visited = _enrich(ccfg, db, lambda: 1000.0, 45, {"enrich_workers": 3, "max_enrich_per_tick": 40})
    assert visited == 12 and FakeWeb.peak > 1  # really concurrent
    homes = [u for u in FakeWeb.fetched if u.endswith(".in/")]
    assert sorted(homes) == sorted(f"https://par{i}.in/" for i in range(12))  # each site's homepage once
    assert db.scalar("SELECT count(*) FROM events WHERE event='enriched'") == 12
    assert db.scalar("SELECT count(*) FROM leads WHERE status='discovered'") == 0


def test_tick_never_overlaps_a_manual_run(ccfg, db):
    db.add_lead(company="Held", website="https://held.in/", domain="held.in")
    with db.lock("run", ttl_seconds=60):
        out = _tick(ccfg, db)
    assert out["pipeline"] == {"skipped": "a pipeline run is in progress"}
    assert FakeDDG.calls == [] and FakeWeb.fetched == []


# ------------------------------------------------------------------ analytics
def test_per_source_stats_daily_and_weekly(ccfg, db, capsys, monkeypatch):
    from app.tools import stats

    for _ in range(3):
        _tick(ccfg, db)
    today = stats.throughput(ccfg, db, 1)["duckduckgo"]
    week = stats.throughput(ccfg, db, 7)["duckduckgo"]
    # 3 ticks x 3 queries = 9 queries; per query 5 DuckDuckGo candidates + 1 Justdial (source "directory")
    assert today["candidates"] == 9 * 5 and today["added"] == _leads(db) == 9 * 3 + 1
    assert today["duplicate"] == 8  # the shared company, seen again by every other query
    assert today["rejected:listing"] == 9  # the listicle, every query
    directory = stats.throughput(ccfg, db, 1)["directory"]
    assert directory["candidates"] == directory["rejected:listing"] == 9  # "Traders in Cuttack - Justdial"
    assert today["enriched"] == 28 and today["qualified"] >= 1 and today["ready_for_review"] >= 1
    assert week == today
    assert stats.daily_added(ccfg, db, 7)[-1][1] == 28

    import main
    from rich.console import Console

    monkeypatch.setattr(main, "console", Console(width=220))  # no truncation in captured output
    main.main(["stats"])
    out = capsys.readouterr().out
    assert "duckduckgo" in out and "listing" in out and "New leads per day" in out


# ------------------------------------------------------------------ unchanged guarantees
def test_quality_and_email_settings_unchanged_in_repo_config():
    s = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
    assert s["sending"]["enabled"] is False and s["sending"]["daily_limit"] == 25
    assert s["qualification"]["min_score"] == 6
    d = s["tick"]["discovery"]
    assert d["queries_per_tick"] > 0 and d["max_new_leads_per_tick"] > 0
    assert not any("day" in k for k in d)  # no daily discovery ceiling
    assert s["research"]["adapters"]["osm"]["enabled"] is False and s["research"]["adapters"]["news_rss"]["enabled"] is False
