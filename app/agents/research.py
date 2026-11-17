"""Research Agent: discovers candidate Indian businesses from free sources.

Sources
  * DuckDuckGo web search (generated ICP queries, Odisha-weighted)
  * IndiaMART / Justdial listings *as they appear in search results*: we read
    the result title only, then search for the company's own website. We do not
    crawl those directories' pages.
  * Directory pages you configure (settings.yaml > research.directory_pages),
    crawled via the robots-respecting fetcher.
  * Optional Google Programmable Search (free tier).

Lead quality gate (a result becomes a lead only if it looks like ONE company's OWN site):
  * skip_domains / skip_tlds (directories, aggregators, wedding/event sites, page builders)
  * listicle / directory titles anywhere in the title ("Top 10 ...", "near me", "Phone Numbers")
  * listing URL paths (/category/, /directory/, /best-x-in-city, -near-me ...)
  * the business-name part of the title must not be a category ("Dental Clinics in Puri",
    "Best Jewellers in Bhilwara", "Wedding Planners in Cuttack"); an SEO tagline after a real
    name is fine ("Sai Dental Care | Best Dental Clinic in Puri")
  * directory listings are kept only if the company's own website is found, and its domain
    must contain the company's name
"""
from __future__ import annotations

import logging
import random
import re
import time
from collections import Counter
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.config import Config
from app.db import DB, loads
from app.tools.domains import host, is_skipped, normalize_url, registered_domain
from app.tools.geo import Geo
from app.tools.http import FetchError, PoliteFetcher, RobotsDisallowed
from app.tools.search import SearchClient, SearchRateLimited

log = logging.getLogger("research")

DIRECTORY_HOSTS = ("justdial.com", "indiamart.com", "tradeindia.com", "sulekha.com")
LISTING_TITLE = re.compile(
    r"^(top|best|find|search|explore|browse|\d+\s)\b|near me|list of|directory|\b\d+\+? best\b|"
    r"\bbest \w+(?: \w+)? in\b|\b(listings?|marketplace|compare)\b", re.I)
# A page about MANY businesses, wherever it appears in the title (taglines included).
LISTICLE_TITLE = re.compile(
    r"\b(?:top|best)\s+\d+\b|\b\d+\+?\s+(?:best|top|popular|famous|leading|trusted)\b|\bnear me\b|\blist of\b|"
    r"\b(?:directory|directories|yellow ?pages|classifieds|listings)\b|"
    r"\b(?:phone numbers?|contact numbers?|reviews? (?:&|and) ratings?|ratings? (?:&|and) reviews?)\b", re.I)
LISTING_PATH = re.compile(
    r"/(?:category|categories|directory|directories|listing|listings|search|tag|tags|vendors?|city|cities|"
    r"near-me|compare)(?:/|$)|(?:^|[/-])(?:top|best)-\d+-|(?:^|/)(?:top|best)-[\w-]+-in-[\w-]+|-near-me\b", re.I)
# Brand names typical of guides/directories ("Dental Clinics in Puri - LocalGuide").
DIRECTORY_BRAND = re.compile(r"guide|director(?:y|ies)|listing|finder|portal|yellow ?pages?|near ?me|reviews|ratings|"
                             r"compare|vendors", re.I)
# Title segments that are page names, not business names ("Contact Us | ABC Traders").
NOISE_SEGMENT = re.compile(r"^(?:home(?: ?page)?|welcome|official(?: web ?site| site)?|index|main|contact(?: us)?|"
                           r"about(?: us)?|services|products|gallery|blog)$", re.I)
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
    # wedding / event directories
    "wedding", "weddings", "venue", "venues", "planner", "planners", "photographer", "photographers",
    "photography", "decorator", "decorators", "decoration", "caterer", "caterers", "banquet", "banquets",
    "hall", "halls", "makeup", "artist", "artists", "mehendi", "bridal", "marriage", "vendor", "vendors",
    "lawn", "lawns", "florist", "florists",
    # listicle filler
    "affordable", "cheap", "famous", "popular", "leading", "trusted", "verified", "local", "nearby", "list",
    "updated", "reviews", "ratings", "numbers", "address", "price", "prices", "cost",
}


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
        excl = cfg.icp.get("query_exclude_sites") or []
        self.exclude = " ".join(f"-site:{d}" for d in excl)
        icp = cfg.icp
        self._places = {w for group in icp["cities"].values() for c in group for w in re.findall(r"[a-z]+", c.lower())}
        self._places |= {w for group in icp.get("states", {}).values() for c in group for w in re.findall(r"[a-z]+", c.lower())}
        vocab = set(GENERIC_WORDS)
        for spec in icp.get("industries", {}).values():
            vocab |= {w for k in spec["keywords"] for w in re.findall(r"[a-z]+", k.lower())}
        vocab |= {w for k in icp.get("query_industries", []) for w in re.findall(r"[a-z]+", k.lower())}
        self._generic = vocab | {w + "s" for w in vocab} | {w + "es" for w in vocab}

    # ------------------------------------------------------------ queries
    @staticmethod
    def _norm(text: str) -> str:
        return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()) + " "

    def build_queries(self) -> list[str]:
        """Least-recently-used rotation over the existing `queries` history: industries and
        cities never searched come first, then the ones searched longest ago. City groups are
        still drawn by icp.yaml city_mix (Odisha first); templates take turns."""
        icp = self.cfg.icp
        cities = icp["cities"]
        mix = {g: w for g, w in icp.get("city_mix", {"odisha": 1.0}).items() if w and cities.get(g)}
        templates, industries = icp["query_templates"], icp["query_industries"]
        history = [(self._norm(r["query"]), r["ran_at"] or "")
                   for r in self.db.query("SELECT query, ran_at FROM queries WHERE source='search'")]

        def last_used(terms: list[str]) -> dict[str, str]:
            last = dict.fromkeys(terms, "")
            for term in terms:
                t = self._norm(term)
                for q, ran in history:
                    if t in q and ran > last[term]:
                        last[term] = ran
            return last

        ind_last = last_used(industries)
        city_last = last_used([c for g in mix for c in cities[g]])
        rng = random.Random()
        queries: list[str] = []
        attempts = 0
        while len(queries) < self.n_queries and attempts < 500:
            attempts += 1
            group = rng.choices(list(mix), weights=list(mix.values()))[0]
            industry = min(industries, key=lambda i: (ind_last[i], rng.random()))
            city = min(cities[group], key=lambda c: (city_last[c], rng.random()))
            template = templates[(len(history) + len(queries) + attempts) % len(templates)]
            q = template.format(industry=industry, city=city)
            ind_last[industry] = city_last[city] = "~"  # just used: sorts after every timestamp
            if q not in queries and not self.db.query_ran(q):
                queries.append(q)
        return queries

    # ------------------------------------------------------------ helpers
    def _q(self, query: str) -> str:
        """Query as sent to the search engine: directories excluded unless the query targets a site."""
        return query if "site:" in query or not self.exclude else f"{query} {self.exclude}"

    @staticmethod
    def _segments(title: str) -> list[str]:
        parts = re.split(r"\s+[|\-–—:»]\s+|\s*\|\s*", title or "")
        return [p.strip(" .,") for p in parts if p.strip(" .,") and not NOISE_SEGMENT.match(p.strip(" .,"))]

    def _is_category(self, name: str) -> bool:
        """'Dental Clinics in Puri', 'Wedding Planners Cuttack': a category, not a business.
        A name that STARTS with a place word is a business ('Sagar Electronics', 'Anand Traders',
        'Cuttack Diagnostic Centre'): categories put the place last."""
        words = [w for w in re.findall(r"[a-z]+", name.lower()) if w not in {"pvt", "ltd", "private", "limited"}]
        if words and words[0] in self._places and words[0] not in self._generic:
            return False
        return not words or all(w in self._generic or w in self._places for w in words)

    def _name_in_domain(self, name: str, url: str) -> bool:
        """True if the site's domain carries the business name (its own website, not a listing)."""
        label = registered_domain(url).split(".")[0].replace("-", "")
        words = [w for w in re.findall(r"[a-z]+", name.lower())
                 if len(w) >= 3 and w not in self._generic and w not in self._places and w not in {"pvt", "ltd"}]
        compact = re.sub(r"[^a-z]", "", name.lower())
        return bool(label) and (any(w in label for w in words) or (len(compact) >= 4 and compact in label))

    @staticmethod
    def listing_reason(title: str, url: str) -> str | None:
        """Why a search result is a list/category page (None = could be one company's own site)."""
        if LISTICLE_TITLE.search(title or ""):
            return "listicle/directory title"
        if LISTING_PATH.search(urlparse(url if "://" in url else "https://" + url).path or ""):
            return "listing URL"
        return None

    def _site_name(self, title: str, url: str) -> str | None:
        """Business name from a search-result title, or None if the title names a category.
        A tagline first ('Best X in City | ABC') is accepted only if ABC is in the domain."""
        for i, seg in enumerate(self._segments(title)):
            if LISTING_TITLE.search(seg) or GOVT_NAME.search(seg) or self._is_category(seg):
                continue
            if i > 0 and (DIRECTORY_BRAND.search(seg) or not self._name_in_domain(seg, url)):
                return None  # category headline + a guide's brand, or a name that isn't their own domain
            return seg[:120]
        return None

    def _directory_name(self, title: str) -> str | None:
        """'ABC Interiors in Saheed Nagar, Bhubaneswar - Justdial' -> 'ABC Interiors'."""
        if LISTING_TITLE.search(title or "") or LISTICLE_TITLE.search(title or ""):
            return None
        t = re.split(r"\s+(?:in|at)\s+[A-Z]|\s+[-|–]\s+|,\s", title)[0].strip()
        if not 2 < len(t) < 80 or GOVT_NAME.search(t) or self._is_category(t):
            return None  # "Diagnostic Centres", "Clinics Rourkela": a category, not a business
        return t

    def _add(self, company: str, url: str | None, source: str, source_url: str, query: str, snippet: str,
             city_hint: str | None = None) -> bool:
        city_hint = city_hint or self.geo.city_from_query(query) or self.geo.city_from_query(snippet)
        if url:
            url = normalize_url(url)
            dom = registered_domain(url)
            if is_skipped(url, self.skip_domains, self.skip_tlds) or self.db.is_suppressed(None, dom):
                return False
            lead_id = self.db.add_lead(company=company or dom, website=url, domain=dom, source=source,
                                       source_url=source_url, query=query, snippet=snippet[:500], city_hint=city_hint)
            if not lead_id:
                existing = self.db.find_lead(domain=dom)
                if existing:
                    self._seen_again(existing, source, source_url, snippet)
        else:
            lead_id = self.db.add_lead(company=company, website=None, domain=None, source=source,
                                       source_url=source_url, query=query, snippet=snippet[:500],
                                       city_hint=city_hint, status="no_website")
        if lead_id:
            self.db.log(self.name, "discovered", lead_id, {"source": source, "query": query, "url": url})
        return bool(lead_id)

    # ------------------------------------------------------ cross-source identity
    @staticmethod
    def _company_key(name: str) -> str:
        n = re.sub(r"\b(pvt|private|ltd|limited|llp|inc|the|co)\b", " ", (name or "").lower())
        return re.sub(r"[^a-z0-9]", "", n)

    def _same_company(self, name: str, city: str | None):
        """An existing lead for the same company name in the same city (it may use another domain)."""
        key = self._company_key(name)
        if not city or len(key) < 4:
            return None
        rows = self.db.query("SELECT * FROM leads WHERE lower(city_hint)=lower(?) OR lower(city)=lower(?)", (city, city))
        return next((r for r in rows if self._company_key(r["company"]) == key), None)

    def _seen_again(self, lead, source: str, source_url: str, evidence: str) -> None:
        """Record another source's sighting of an existing lead (once per source)."""
        if (lead["source"] or "") == source:
            return
        for e in self.db.query("SELECT detail FROM events WHERE lead_id=? AND event='seen_again'", (lead["id"],)):
            if (loads(e["detail"], {}) or {}).get("source") == source:
                return
        self.db.log(self.name, "seen_again", lead["id"],
                    {"source": source, "source_url": source_url, "evidence": (evidence or "")[:300]})

    def ingest(self, c, budget: dict) -> str:
        """One candidate from a source adapter -> 'added' | 'seen_again' | 'unresolved' | 'rejected:<why>'.
        Same gate as search results; budget['resolutions'] limits website lookups (DuckDuckGo)."""
        name = " ".join((c.company or "").split())
        if not name or LISTING_TITLE.search(name) or GOVT_NAME.search(name) or self._is_category(name):
            return "rejected:name"
        url = c.url
        if not url:  # a link in the source item whose domain carries the company's name
            url = next((u for u in c.links if not is_skipped(u, self.skip_domains, self.skip_tlds)
                        and not self.listing_reason("", u) and self._name_in_domain(name, u)), None)
        if not url and c.resolve and budget.get("resolutions", 0) > 0:
            budget["resolutions"] -= 1
            try:
                url = self._resolve_website(name, c.city_hint)
            except SearchRateLimited:
                budget["resolutions"] = 0
        if not url:
            return "unresolved"
        if is_skipped(url, self.skip_domains, self.skip_tlds) or self.listing_reason("", url):
            return "rejected:url"
        existing = self.db.find_lead(domain=registered_domain(url)) or self._same_company(name, c.city_hint)
        if existing:
            self._seen_again(existing, c.source, c.source_url, c.evidence)
            return "seen_again"
        added = self._add(name, url, c.source, c.source_url, c.query, c.evidence, city_hint=c.city_hint)
        return "added" if added else "rejected:url"

    def run_sources(self, budget: int | None = None, deadline: float | None = None, only: str | None = None) -> dict:
        """Run the enabled source adapters (config: research.adapters). Disabled ones are skipped."""
        from app.sources import build_adapters

        budget = self.max_new if budget is None else budget
        out: dict[str, dict] = {}
        for adapter in build_adapters(self.cfg, self.db, self.fetcher, self.geo):
            if only and adapter.name != only:
                continue
            if not adapter.enabled:
                out[adapter.name] = {"skipped": "disabled"}
                continue
            counts: Counter = Counter()
            lookups = {"resolutions": adapter.resolutions_left()}
            start = lookups["resolutions"]
            for cand in adapter.discover(deadline):
                if budget <= 0 or (deadline is not None and time.monotonic() > deadline):
                    break
                outcome = self.ingest(cand, lookups)
                counts[outcome.split(":")[0]] += 1
                budget -= outcome == "added"
            adapter.spend_resolutions(start - lookups["resolutions"])
            out[adapter.name] = dict(counts)
            log.info("Source %s: %s", adapter.name, dict(counts) or "nothing new")
        return out

    def _resolve_website(self, name: str, city: str | None) -> str | None:
        """The listing's own website: its domain must carry the company's name."""
        q = f"\"{name}\" {city or ''} official website".strip()
        for r in self.search.search(self._q(q), 5):
            url = r["url"]
            if (url and not is_skipped(url, self.skip_domains, self.skip_tlds)
                    and not self.listing_reason(r["title"], url) and self._name_in_domain(name, url)):
                return url
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
                results = self.search.search(self._q(q), self.per_query)
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
                    name = None if self.listing_reason(r["title"], r["url"]) else self._site_name(r["title"], r["url"])
                    if not name:
                        log.debug("skip (listing/category): %s %s", r["title"], r["url"])
                        continue  # "Best jewellers in X", "Top 10 ...", /category/...: a list page, not a business
                    added += self._add(name, r["url"], r["engine"], r["url"], q, r["snippet"])
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
        sources = self.run_sources(budget - added)
        added += sum(v.get("added", 0) for v in sources.values())
        log.info("Research Agent: %d new leads", added)
        out = {"new_leads": added}
        out.update({f"source:{k}": v.get("added", 0) for k, v in sources.items() if "skipped" not in v})
        return out
