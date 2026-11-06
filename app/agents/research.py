"""Research Agent: discovers candidate Indian businesses from free sources.

Sources
  * DuckDuckGo web search (generated ICP queries, Odisha-weighted)
  * IndiaMART / Justdial listings *as they appear in search results*: we read
    the result title only, then search for the company's own website. We do not
    crawl those directories' pages.
  * Directory pages you configure (settings.yaml > research.directory_pages),
    crawled via the robots-respecting fetcher.
  * Optional Google Programmable Search (free tier).
"""
from __future__ import annotations

import logging
import random
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import Config
from app.db import DB
from app.tools.domains import host, is_skipped, normalize_url, registered_domain
from app.tools.geo import Geo
from app.tools.http import FetchError, PoliteFetcher, RobotsDisallowed
from app.tools.search import SearchClient, SearchRateLimited

log = logging.getLogger("research")

DIRECTORY_HOSTS = ("justdial.com", "indiamart.com", "tradeindia.com", "sulekha.com")
LISTING_TITLE = re.compile(r"^(top|best|\d+\s)|near me|list of|directory", re.I)
GOVT_NAME = re.compile(r"^(govt\.?|government|district|state|municipal|public)\b", re.I)
# Words that, on their own, describe a category rather than name a business.
GENERIC_WORDS = {
    "the", "and", "of", "in", "at", "for", "best", "top", "near", "me", "my", "all", "new", "india", "indian",
    "centre", "centres", "center", "centers", "clinic", "clinics", "diagnostic", "diagnostics", "hospital",
    "hospitals", "lab", "labs", "laboratory", "pathology", "services", "service", "solutions", "shop", "shops",
    "store", "stores", "dealers", "dealer", "suppliers", "supplier", "manufacturers", "manufacturer",
    "distributors", "distributor", "traders", "agency", "agencies", "institute", "institutes", "coaching",
    "classes", "tuition", "school", "schools", "academy", "hotel", "hotels", "restaurant", "restaurants",
    "travel", "travels", "tours", "builders", "developers", "real", "estate", "interior", "interiors",
    "designers", "furniture", "jewellers", "showroom", "showrooms", "consultants", "ca", "firm", "firms",
    "logistics", "transport", "packers", "movers", "event", "events", "management", "company", "companies",
}
TITLE_NOISE = re.compile(r"\s*[|\-–—:]\s*(home|official( website)?|welcome).*$", re.I)


class ResearchAgent:
    name = "research"

    def __init__(self, cfg: Config, db: DB, fetcher: PoliteFetcher, search: SearchClient, geo: Geo):
        self.cfg, self.db, self.fetcher, self.search, self.geo = cfg, db, fetcher, search, geo
        r = cfg.settings["research"]
        self.max_new = int(r.get("max_new_leads_per_run", 30))
        self.n_queries = int(r.get("queries_per_run", 8))
        self.per_query = int(r.get("results_per_query", 10))
        self.resolve_dirs = bool(r.get("resolve_directory_listings", True))
        self.keep_unresolved = bool(r.get("keep_listings_without_website", False))
        self.skip_domains = cfg.settings.get("skip_domains", []) + [p["domain"] for p in cfg.portfolio]
        self.skip_tlds = cfg.settings.get("skip_tlds", [])

    # ------------------------------------------------------------ queries
    def build_queries(self) -> list[str]:
        icp = self.cfg.icp
        cities = icp["cities"]
        mix = icp.get("city_mix", {"odisha": 1.0})
        templates, industries = icp["query_templates"], icp["query_industries"]
        rng = random.Random()
        queries: list[str] = []
        attempts = 0
        while len(queries) < self.n_queries and attempts < 500:
            attempts += 1
            group = rng.choices(list(mix), weights=list(mix.values()))[0]
            q = rng.choice(templates).format(industry=rng.choice(industries), city=rng.choice(cities[group]))
            if q not in queries and not self.db.query_ran(q):
                queries.append(q)
        return queries

    # ------------------------------------------------------------ helpers
    def _company_from_title(self, title: str) -> str:
        t = TITLE_NOISE.sub("", title or "").strip()
        for sep in (" | ", " - ", " – ", " — ", ": "):
            if sep in t:
                t = t.split(sep)[0].strip()
        return t[:120]

    def _directory_name(self, title: str) -> str | None:
        """'ABC Interiors in Saheed Nagar, Bhubaneswar - Justdial' -> 'ABC Interiors'."""
        if LISTING_TITLE.search(title or ""):
            return None
        t = re.split(r"\s+(?:in|at)\s+[A-Z]|\s+[-|–]\s+|,\s", title)[0].strip()
        if not 2 < len(t) < 80 or GOVT_NAME.search(t):
            return None
        words = [w for w in re.findall(r"[a-z]+", t.lower()) if w not in {"pvt", "ltd", "private", "limited"}]
        cities = {c.lower() for group in self.cfg.icp["cities"].values() for c in group}
        if not words or all(w in GENERIC_WORDS or w in cities for w in words):
            return None  # "Diagnostic Centres", "Clinics Rourkela": a category, not a business
        return t

    def _add(self, company: str, url: str | None, source: str, source_url: str, query: str, snippet: str) -> bool:
        city_hint = self.geo.city_from_query(query) or self.geo.city_from_query(snippet)
        if url:
            url = normalize_url(url)
            dom = registered_domain(url)
            if is_skipped(url, self.skip_domains, self.skip_tlds) or self.db.is_suppressed(None, dom):
                return False
            lead_id = self.db.add_lead(company=company or dom, website=url, domain=dom, source=source,
                                       source_url=source_url, query=query, snippet=snippet[:500], city_hint=city_hint)
        else:
            lead_id = self.db.add_lead(company=company, website=None, domain=None, source=source,
                                       source_url=source_url, query=query, snippet=snippet[:500],
                                       city_hint=city_hint, status="no_website")
        if lead_id:
            self.db.log(self.name, "discovered", lead_id, {"source": source, "query": query, "url": url})
        return bool(lead_id)

    def _resolve_website(self, name: str, city: str | None) -> str | None:
        q = f"\"{name}\" {city or ''} official website".strip()
        for r in self.search.search(q, 5):
            if r["url"] and not is_skipped(r["url"], self.skip_domains, self.skip_tlds):
                words = [w for w in re.findall(r"[a-z]{3,}", name.lower()) if w not in {"pvt", "ltd", "the", "and"}]
                dom = registered_domain(r["url"])
                if any(w in dom for w in words) or any(w in r["title"].lower() for w in words[:2]):
                    return r["url"]
        return None

    # ---------------------------------------------------------------- run
    def run_search(self, budget: int, queries: list[str] | None = None, deadline: float | None = None) -> int:
        """deadline: time.monotonic() value after which no new search is started (serverless ticks)."""
        import time as _time

        out_of_time = lambda: deadline is not None and _time.monotonic() > deadline  # noqa: E731
        added = 0
        for q in (self.build_queries() if queries is None else queries):
            if added >= budget or out_of_time():
                break
            try:
                results = self.search.search(q, self.per_query)
            except SearchRateLimited as e:
                log.warning("Search rate limit hit, stopping research for this run: %s", e)
                break
            self.db.record_query(q, "search", len(results))
            log.info("Query %r -> %d results", q, len(results))
            for r in results:
                if added >= budget:
                    break
                h = host(r["url"])
                if any(h.endswith(d) for d in DIRECTORY_HOSTS):
                    name = self._directory_name(r["title"])
                    if not name:
                        continue
                    site = None
                    if self.resolve_dirs and not out_of_time():
                        try:
                            site = self._resolve_website(name, self.geo.city_from_query(q))
                        except SearchRateLimited:
                            break
                    if not site and not self.keep_unresolved:
                        continue  # no website of its own -> no email to find; don't spend the lead budget
                    added += self._add(name, site, f"directory:{registered_domain(r['url'])}", r["url"], q, r["snippet"])
                else:
                    added += self._add(self._company_from_title(r["title"]), r["url"], r["engine"], r["url"], q, r["snippet"])
        return added

    def run_directories(self, budget: int) -> int:
        added = 0
        for page_url in self.cfg.settings["research"].get("directory_pages") or []:
            if added >= budget:
                break
            try:
                page = self.fetcher.fetch(page_url)
            except (RobotsDisallowed, FetchError) as e:
                log.warning("Directory %s skipped: %s", page_url, e)
                continue
            soup = BeautifulSoup(page.html, "lxml")
            own = registered_domain(page_url)
            for a in soup.find_all("a", href=True):
                url = urljoin(page.final_url, a["href"])
                if not url.startswith("http") or registered_domain(url) == own:
                    continue
                name = " ".join(a.get_text(" ").split()) or registered_domain(url)
                added += self._add(name[:120], url, "directory_page", page_url, page_url, "")
                if added >= budget:
                    break
        return added

    def add_manual(self, url: str, company: str | None = None, city: str | None = None) -> int | None:
        url = normalize_url(url)
        dom = registered_domain(url)
        lead_id = self.db.add_lead(company=company or dom, website=url, domain=dom, source="manual",
                                   source_url=url, query="", snippet="", city_hint=city)
        if lead_id:
            self.db.log(self.name, "discovered", lead_id, {"source": "manual", "url": url})
        return lead_id

    def run(self, max_new: int | None = None) -> dict:
        budget = max_new or self.max_new
        added = self.run_directories(budget)
        added += self.run_search(budget - added)
        log.info("Research Agent: %d new leads", added)
        return {"new_leads": added}
