"""Source adapters (OSM Overpass, news RSS): off by default, terms gate, conservative limits,
normalisation into the lead schema, cross-source dedupe + seen_again, LRU query rotation.
Fully offline: a fake fetcher serves synthetic fixtures."""
from __future__ import annotations

import functools
import http.server
import json
import threading
from pathlib import Path

import pytest
import yaml

from app.tools.http import FetchError, Page, RobotsDisallowed

FIX = Path(__file__).parent / "fixtures" / "sources"
ROOT = Path(__file__).resolve().parent.parent
OVERPASS = (FIX / "overpass_city.json").read_text()
FEED = (FIX / "news_feed.xml").read_text()
TEST_FEED = {"name": "testfeed", "url": "https://news.example.in/feed/", "terms": "permitted"}


class FakeFetcher:
    """Stands in for PoliteFetcher; records every request, never touches the network."""

    def __init__(self, bodies: dict[str, str] | None = None, error: Exception | None = None):
        self.bodies, self.error, self.calls = bodies or {}, error, []

    def fetch(self, url, render=None, *, accept=("html",), cache_hours=None, retries=3, timeout=None):
        self.calls.append({"url": url, "accept": accept, "retries": retries, "cache_hours": cache_hours})
        if self.error:
            raise self.error
        for key, body in self.bodies.items():
            if key in url:
                return Page(url, url, 200, body, "application/json" if "json" in accept else "application/rss+xml")
        raise FetchError(f"{url}: HTTP 404")

    def close(self):
        pass


class FakeSearch:
    def __init__(self, results: dict[str, list] | None = None):
        self.results, self.queries = results or {}, []

    def search(self, q, n):
        self.queries.append(q)
        return next((v for k, v in self.results.items() if k in q), [])


def _agent(cfg, db, fetcher, search=None):
    from app.agents.research import ResearchAgent
    from app.tools.geo import Geo

    return ResearchAgent(cfg, db, fetcher=fetcher, search=search or FakeSearch(), geo=Geo(cfg))


def _enable(cfg, name, **conf):
    cfg.settings["research"]["adapters"][name].update(enabled=True, **conf)


@pytest.fixture()
def osm_cfg(cfg):
    _enable(cfg, "osm")
    cfg.icp["city_mix"] = {"odisha": 1.0}
    cfg.icp["cities"]["odisha"] = ["Bhubaneswar", "Cuttack"]
    return cfg


@pytest.fixture()
def news_cfg(cfg):
    _enable(cfg, "news_rss", feeds=[TEST_FEED], max_resolutions_per_day=1)
    return cfg


# ------------------------------------------------------------------ safety defaults
def test_repo_defaults_keep_every_source_off_and_no_feed_permitted():
    s = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
    adapters = s["research"]["adapters"]
    assert all(v["enabled"] is False for v in adapters.values())
    terms = {f["name"]: f["terms"] for f in adapters["news_rss"]["feeds"]}
    assert terms["inc42"] == terms["entrackr"] == "prohibited"
    assert "permitted" not in terms.values()
    assert s["sending"]["enabled"] is False
    assert {"inc42.com", "yourstory.com", "entrackr.com", "openstreetmap.org"} <= set(s["skip_domains"])


def test_disabled_sources_never_fetch(cfg, db):
    f = FakeFetcher({"": OVERPASS})
    out = _agent(cfg, db, f).run_sources()
    assert out == {"osm": {"skipped": "disabled"}, "news_rss": {"skipped": "disabled"}}
    assert f.calls == [] and db.all_leads() == []


def test_unpermitted_feeds_are_never_fetched(cfg, db):
    _enable(cfg, "news_rss")  # repo feeds: prohibited / unverified
    f = FakeFetcher({"": FEED})
    assert _agent(cfg, db, f).run_sources(only="news_rss") == {"news_rss": {}}
    assert f.calls == []


# ------------------------------------------------------------------ OSM
def test_osm_query_is_bounded_and_escaped(osm_cfg, db):
    from app.sources.osm import OsmAdapter

    q = OsmAdapter(osm_cfg, db, None, None).build_query('Puri "x"')
    assert q.startswith("[out:json][timeout:60];") and q.rstrip().endswith("out tags center 150;")
    assert '"ISO3166-1"="IN"' in q and '["name"="Puri \\"x\\""]' in q
    assert '["website"]["office"]' in q and '["contact:website"]["shop"]' in q


def test_osm_results_normalised_gated_and_attributed(osm_cfg, db):
    f = FakeFetcher({"overpass-api.de": OVERPASS})
    out = _agent(osm_cfg, db, f).run_sources()
    assert out["osm"] == {"added": 3, "rejected": 2}  # justdial URL + "Dental Clinics" category name
    call = f.calls[0]
    assert call["url"].startswith("https://overpass-api.de/api/interpreter?data=")
    assert call["accept"] == ("json",) and call["retries"] == 1 and call["cache_hours"] == 168
    leads = {l["domain"]: l for l in db.all_leads()}
    assert set(leads) == {"kalingasteelfab.in", "utkaldentalcare.com", "odisharicemills.co.in"}
    k = leads["kalingasteelfab.in"]
    assert k["source"] == "osm" and k["source_url"] == "https://www.openstreetmap.org/node/1001"
    assert "© OpenStreetMap contributors (ODbL)" in k["snippet"] and "office=company" in k["snippet"]
    assert k["city_hint"] in ("Bhubaneswar", "Cuttack") and k["query"] == f"osm:{k['city_hint']}"
    assert leads["utkaldentalcare.com"]["website"] == "https://utkaldentalcare.com/"
    assert leads["odisharicemills.co.in"]["website"] == "http://odisharicemills.co.in/"


def test_osm_daily_cap_and_lru_city_rotation(osm_cfg, db):
    from app.sources.osm import OsmAdapter

    _enable(osm_cfg, "osm", max_requests_per_day=2, min_minutes_between_requests=0)
    f = FakeFetcher({"overpass-api.de": '{"elements": []}'})
    agent = _agent(osm_cfg, db, f)
    for _ in range(3):
        agent.run_sources()
    assert len(f.calls) == 2  # third run: today's cap reached
    asked = {r["query"] for r in db.query("SELECT query FROM queries WHERE source='osm'")}
    assert asked == {"osm:Bhubaneswar", "osm:Cuttack"}  # least-recently-used city first
    assert OsmAdapter(osm_cfg, db, f, None).requests_today() == 2
    assert db.queries_since("2000-01-01", source="search") == 0  # doesn't eat the search quota


def test_osm_minimum_gap_between_requests(osm_cfg, db):
    f = FakeFetcher({"overpass-api.de": '{"elements": []}'})
    agent = _agent(osm_cfg, db, f)
    agent.run_sources()
    agent.run_sources()
    assert len(f.calls) == 1  # default: 30 min between requests
    db.execute("UPDATE queries SET ran_at='2000-01-01T00:00:00+00:00' WHERE source='osm'")
    agent.run_sources()
    assert len(f.calls) == 2


@pytest.mark.parametrize("error", [FetchError("HTTP 429"), RobotsDisallowed("robots"), FetchError("timed out")])
def test_osm_backs_off_after_failure(osm_cfg, db, error):
    from app.sources.osm import OsmAdapter

    f = FakeFetcher(error=error)
    agent = _agent(osm_cfg, db, f)
    agent.run_sources()
    agent.run_sources()
    assert len(f.calls) == 1  # second run waits out the back-off
    assert "backing off" in OsmAdapter(osm_cfg, db, f, None).status()["backoff"]


# ------------------------------------------------------------------ news RSS
@pytest.mark.parametrize("title,name", [
    ("AI CRM startup Dreamco raises $5 Mn from Example Ventures", "Dreamco"),
    ("Exclusive: Shree Example Foods raises Rs 110 Cr", "Shree Example Foods"),
    ("Wealthtech startup finny.example raises Rs 5 Cr in angel round", "finny.example"),
    ("Example Fund leads Rs 10 Cr seed round in deep-tech startup CountX", "CountX"),
    ("How Volks Example carved its space in India's EPC market", "Volks Example"),
    ("Kutumbx parent posts Rs 405 Cr revenue in FY26", "Kutumbx"),
    ("Dreamco Bags $53 Mn To Disrupt The CRM Space", "Dreamco"),
])
def test_company_extracted_from_headline(title, name):
    from app.sources.news_rss import company_from_headline

    assert company_from_headline(title) == name


@pytest.mark.parametrize("title", [
    "Startup news and updates: daily roundup (November 17, 2026)",
    "Aman Example, Ranveer Example launch men's grooming brand",
    "Example Capital closes Rs 500 Cr fund to back startups",
    "Northwest closes $4 Bn global fund",
    "Govt opens applications for new MSME scheme",
    "This Mumbai-based company is building a beauty brand",
    "Only 1% of Indian MSMEs have a working website, says study",
    "Kisshtx's NBFC arm Example gets rating upgrade",
])
def test_headlines_without_one_company_are_ignored(title):
    from app.sources.news_rss import company_from_headline

    assert company_from_headline(title) is None


def test_permitted_feed_companies_resolved_and_saved_with_provenance(news_cfg, db):
    f = FakeFetcher({"news.example.in/feed": FEED})
    search = FakeSearch({"Odicraft": [{"title": "Odicraft | Handloom from Odisha", "url": "https://www.odicraft.com/",
                                       "snippet": "", "engine": "duckduckgo"}]})
    out = _agent(news_cfg, db, f, search).run_sources(only="news_rss")
    # Krishimitra: own link in the item; Odicraft: one DuckDuckGo lookup (the daily budget);
    # Paylakshmi: budget spent -> unresolved; Acmebots: foreign; govt/fund headlines: no company.
    assert out["news_rss"] == {"added": 2, "unresolved": 1}
    assert f.calls[0]["accept"] == ("xml", "rss", "atom")
    assert len(search.queries) == 1 and "-site:justdial.com" in search.queries[0]
    leads = {l["domain"]: l for l in db.all_leads()}
    assert set(leads) == {"krishimitra.in", "odicraft.com"}  # never the article's own site
    k = leads["krishimitra.in"]
    assert k["source"] == "news:testfeed" and k["source_url"] == "https://news.example.in/2026/11/krishimitra-seed/"
    assert k["city_hint"] == "Bhubaneswar" and "raises Rs 5 Cr" in k["snippet"] and k["query"] == "rss:testfeed"
    assert k["status"] == "discovered"  # goes through enrichment -> qualification like every other lead

    from app.sources.news_rss import NewsRssAdapter
    assert NewsRssAdapter(news_cfg, db, f, None).resolutions_left() == 0
    _agent(news_cfg, db, f, search).run_sources(only="news_rss")
    assert len(f.calls) == 1  # min_hours_between_fetches: not fetched again


# ------------------------------------------------------------------ dedupe + evidence
def test_cross_source_duplicates_become_seen_again_events(osm_cfg, db):
    db.add_lead(company="Kalinga Steel Fabricators", website="https://kalingasteelfab.in/", domain="kalingasteelfab.in",
                source="duckduckgo", source_url="https://kalingasteelfab.in/", city_hint="Bhubaneswar")
    db.add_lead(company="Utkal Dental Care Pvt Ltd", website="https://utkaldental.in/", domain="utkaldental.in",
                source="duckduckgo", source_url="https://utkaldental.in/", city_hint="Bhubaneswar")
    osm_cfg.icp["cities"]["odisha"] = ["Bhubaneswar"]
    f = FakeFetcher({"overpass-api.de": OVERPASS})
    out = _agent(osm_cfg, db, f).run_sources()
    assert out["osm"]["seen_again"] == 2 and out["osm"]["added"] == 1  # same domain; same name + city
    ev = db.query("SELECT lead_id, detail FROM events WHERE event='seen_again' ORDER BY lead_id")
    details = [json.loads(e["detail"]) for e in ev]
    assert {d["source"] for d in details} == {"osm"} and all("openstreetmap.org" in d["source_url"] for d in details)
    assert all("ODbL" in d["evidence"] for d in details)

    db.execute("DELETE FROM http_cache")
    db.execute("DELETE FROM queries")
    _agent(osm_cfg, db, f).run_sources()  # the same source seeing it again adds no new event
    assert db.scalar("SELECT count(*) FROM events WHERE event='seen_again'") == 2
    assert db.scalar("SELECT count(*) FROM leads") == 3


def test_search_duplicate_from_same_engine_logs_nothing(cfg, db):
    class Search:
        def search(self, q, n):
            return [{"title": "Sai Clinic", "url": "https://saiclinic.in/", "snippet": "", "engine": "duckduckgo"}]

    a = _agent(cfg, db, FakeFetcher(), Search())
    a.run_search(5, ["clinic Puri"])
    a.run_search(5, ["clinic Cuttack"])
    assert db.scalar("SELECT count(*) FROM events WHERE event='seen_again'") == 0


# ------------------------------------------------------------------ query rotation
def test_query_rotation_prefers_least_recently_used(cfg, db):
    cfg.settings["research"]["queries_per_run"] = 4
    cfg.icp["city_mix"] = {"odisha": 1.0}
    cfg.icp["cities"]["odisha"] = ["Puri", "Cuttack", "Rourkela", "Balasore", "Sambalpur"]
    cfg.icp["query_industries"] = ["clinic", "distributor", "school", "rice mill", "printing press"]
    for q in ("clinic Puri \"contact us\"", "distributor Cuttack \"about us\""):
        db.record_query(q, "search", 3)
    qs = _agent(cfg, db, FakeFetcher()).build_queries()
    assert len(qs) == 4
    used = [next(i for i in cfg.icp["query_industries"] if f" {i} " in f" {q.replace(chr(34), ' ')} ") for q in qs]
    assert len(set(used)) == 4 and "clinic" not in used[:3] and "distributor" not in used[:3]
    assert not any("Puri" in q or "Cuttack" in q for q in qs[:3])


# ------------------------------------------------------------------ fetcher, tick, CLI
def test_fetcher_feeds_are_opt_in(cfg, db, tmp_path):
    from app.tools.http import PoliteFetcher

    (tmp_path / "robots.txt").write_text("User-agent: *\nDisallow: /private/\n")
    (tmp_path / "feed.xml").write_text(FEED)
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "feed.xml").write_text(FEED)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a, **k: None  # type: ignore[assignment]
    srv = http.server.ThreadingHTTPServer(("localhost", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://localhost:{srv.server_address[1]}"
    try:
        f = PoliteFetcher(cfg, db)
        with pytest.raises(FetchError, match="not HTML"):
            f.fetch(base + "/feed.xml", cache_hours=0)  # HTML-only by default, as before
        assert "<rss" in f.fetch(base + "/feed.xml", accept=("xml",), cache_hours=0).html
        with pytest.raises(RobotsDisallowed):
            f.fetch(base + "/private/feed.xml", accept=("xml",))
    finally:
        srv.shutdown()


def test_tick_runs_only_enabled_sources(osm_cfg, db, monkeypatch):
    import app.graph
    from app.tick import tick

    f = FakeFetcher({"overpass-api.de": OVERPASS})
    monkeypatch.setattr(app.graph, "PoliteFetcher", lambda cfg, db: f)
    osm_cfg.settings["research"]["queries_per_run"] = 0
    out = tick(osm_cfg, db, force=True)
    assert out["sources"] == {"osm": 3} and out["pipeline"]["new_leads"] == 0  # no search queries configured
    osm_cfg.settings["research"]["adapters"]["osm"]["enabled"] = False
    db.kv_set("tick_last", "")
    assert "sources" not in tick(osm_cfg, db, force=True)


def test_cli_sources_and_research_source(cfg, db, capsys):
    import main

    main.main(["sources"])
    out = capsys.readouterr().out
    assert "osm" in out and "news_rss" in out and "prohibited" in out and "deferred" in out
    main.main(["research", "--source", "osm"])
    assert "disabled" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main.main(["research", "--source", "nope"])
