"""OpenStreetMap local-business discovery through the public Overpass API (free, no key).

For one ICP city per request, asks for businesses that list their own website
(`website` / `contact:website` tags) in ICP-relevant categories. Conservative by design:

  * a few requests per day (research.adapters.osm.max_requests_per_day), one per run,
    cities rotated least-recently-used (Odisha weighted first via icp.yaml city_mix)
  * responses cached for a week; robots.txt, User-Agent and per-host delay via PoliteFetcher
  * no in-call retry loop: a 429/5xx/timeout puts the whole adapter into exponential back-off
  * attribution "© OpenStreetMap contributors (ODbL)" is kept in every lead's evidence

Overpass usage policy: https://wiki.openstreetmap.org/wiki/Overpass_API (regular applications
should stay far below its 10,000 requests/day guideline; heavy users should self-host).
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Iterator
from urllib.parse import urlencode

from app.sources import Candidate, SourceAdapter
from app.tools.http import FetchError, RobotsDisallowed

log = logging.getLogger("sources.osm")
ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
KIND_KEYS = ("office", "shop", "craft", "healthcare", "industrial", "amenity", "tourism")
DEFAULT_FILTERS = ['["office"]', '["shop"]', '["craft"]', '["healthcare"]', '["industrial"]',
                   '["amenity"~"^(clinic|dentist|doctors|hospital|school|college|training|driving_school)$"]']


class OsmAdapter(SourceAdapter):
    name = "osm"

    @property
    def endpoint(self) -> str:
        return self.conf.get("endpoint") or "https://overpass-api.de/api/interpreter"

    def build_query(self, city: str) -> str:
        timeout = int(self.conf.get("timeout_seconds", 60))
        limit = int(self.conf.get("max_results_per_request", 150))
        esc = city.replace("\\", "\\\\").replace('"', '\\"')
        filters = self.conf.get("filters") or DEFAULT_FILTERS
        selectors = [f'  nwr(area.city)["{key}"]{f};' for key in ("website", "contact:website") for f in filters]
        return (f'[out:json][timeout:{timeout}];\n'
                f'area["ISO3166-1"="IN"][admin_level=2]->.in;\n'
                f'rel(area.in)["boundary"="administrative"]["name"="{esc}"];\n'
                f'map_to_area->.city;\n(\n' + "\n".join(selectors) + f'\n);\nout tags center {limit};')

    def next_city(self) -> str:
        """Least-recently-queried city, group picked by icp.yaml city_mix (zero-weight groups skipped)."""
        icp = self.cfg.icp
        mix = {g: w for g, w in (icp.get("city_mix") or {"odisha": 1.0}).items() if w and icp["cities"].get(g)}
        group = random.choices(list(mix), weights=list(mix.values()))[0]
        last = {r["query"]: r["ran_at"] or "" for r in
                self.db.query("SELECT query, ran_at FROM queries WHERE source=?", (self.name,))}
        return min(icp["cities"][group], key=lambda c: (last.get(f"osm:{c}", ""), random.random()))

    def candidate(self, el: dict, city: str) -> Candidate | None:
        tags = el.get("tags") or {}
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        site = (tags.get("website") or tags.get("contact:website") or "").split(";")[0].strip()
        if not name or not site or " " in site:
            return None
        if "://" not in site:
            site = "https://" + site
        kind = next((f"{k}={tags[k]}" for k in KIND_KEYS if k in tags), "")
        addr = ", ".join(x for x in (tags.get("addr:street"), tags.get("addr:city") or city, tags.get("addr:postcode")) if x)
        return Candidate(
            company=name[:120], url=site, source=self.name,
            source_url=f"https://www.openstreetmap.org/{el.get('type', 'node')}/{el.get('id')}",
            evidence=f"OSM {kind}: {name} · {addr} · {ATTRIBUTION}"[:500],
            city_hint=city, query=f"osm:{city}")

    def discover(self, deadline: float | None = None) -> Iterator[Candidate]:
        if not self.enabled:
            return
        why = self.cooling_down()
        if why:
            log.info("osm: %s", why)
            return
        per_day = int(self.conf.get("max_requests_per_day", 4))
        for _ in range(int(self.conf.get("max_requests_per_run", 1))):
            if self.requests_today() >= per_day or (deadline is not None and time.monotonic() > deadline):
                return
            city = self.next_city()
            url = self.endpoint + "?" + urlencode({"data": self.build_query(city)})
            self.db.record_query(f"osm:{city}", self.name, -1)  # counts towards today's cap even if it fails
            try:
                page = self.fetcher.fetch(url, accept=("json",), retries=1,
                                          cache_hours=float(self.conf.get("cache_hours", 168)),
                                          timeout=float(self.conf.get("timeout_seconds", 60)) + 15)
                elements = json.loads(page.html).get("elements") or []
            except RobotsDisallowed:
                self.back_off("robots.txt disallows the Overpass endpoint (or it could not be read)")
                return
            except (FetchError, ValueError) as e:
                self.back_off(f"Overpass request failed: {e}")
                return
            self.reset_backoff()
            self.db.record_query(f"osm:{city}", self.name, len(elements))
            log.info("osm: %s -> %d elements", city, len(elements))
            for el in elements:
                c = self.candidate(el, city)
                if c:
                    yield c
