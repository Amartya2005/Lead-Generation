"""Extra lead-discovery sources, each an independent adapter that is OFF by default.

An adapter only *finds* candidates. The Research Agent's `ingest()` puts every candidate
through the same quality gate as search results (skip-listed domains, listing pages,
category names), resolves a missing website with the same strict name-in-domain rule,
deduplicates across sources and saves it with its provenance:

    source      adapter + feed, e.g. "osm" or "news:inc42"
    source_url  where it was found (OSM element, article)
    query       what the source was asked ("osm:Bhubaneswar", "rss:inc42")
    snippet     evidence shown to the reviewer;  created_at = discovery date

Configure in config/settings.yaml -> research.adapters.<name>. Adding a source = a new
module with a SourceAdapter subclass, listed in build_adapters().
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

from app.config import Config
from app.db import DB

log = logging.getLogger("sources")

# Sources considered and deliberately not implemented, shown by `python main.py sources`.
DEFERRED = {
    "x": "deferred: no free, compliant read access (official API is pay-per-use; scraping breaks X's terms)",
    "google_places": "not implemented: Enterprise fields (website/phone) are billed and Places content can't be stored",
}


@dataclass
class Candidate:
    company: str
    url: str | None            # the company's own website, if the source gives one
    source: str
    source_url: str
    evidence: str
    city_hint: str | None = None
    query: str = ""
    links: list[str] = field(default_factory=list)  # outbound links that may be the official site
    resolve: bool = False      # no website: may be looked up (DuckDuckGo, strict name-in-domain)


class SourceAdapter:
    name = ""

    def __init__(self, cfg: Config, db: DB, fetcher, geo):
        self.cfg, self.db, self.fetcher, self.geo = cfg, db, fetcher, geo
        self.conf: dict = (cfg.settings["research"].get("adapters") or {}).get(self.name) or {}

    @property
    def enabled(self) -> bool:
        return self.conf.get("enabled") is True

    def discover(self, deadline: float | None = None) -> Iterator[Candidate]:
        raise NotImplementedError

    def status(self) -> dict:
        return {"enabled": self.enabled, "requests_today": self.requests_today(), "backoff": self.cooling_down() or ""}

    # ------------------------------------------------ usage accounting (existing tables)
    def day_start(self) -> str:
        tz = ZoneInfo(self.cfg.settings["sending"].get("send_window", {}).get("timezone", "Asia/Kolkata"))
        start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return start.astimezone(timezone.utc).isoformat(timespec="seconds")

    def requests_today(self) -> int:
        """Requests are recorded in the existing `queries` table with source=<adapter name>."""
        return self.db.scalar("SELECT count(*) FROM queries WHERE source=? AND ran_at>=?", (self.name, self.day_start()))

    def last_run(self, key: str) -> str | None:
        return self.db.scalar("SELECT ran_at FROM queries WHERE query=?", (key,))

    def resolutions_left(self) -> int:
        cap = int(self.conf.get("max_resolutions_per_day", 0))
        used = int(self.db.kv_get(f"source:{self.name}:resolutions:{self.day_start()[:10]}", "0") or 0)
        return max(0, cap - used)

    def spend_resolutions(self, n: int) -> None:
        if n > 0:
            key = f"source:{self.name}:resolutions:{self.day_start()[:10]}"
            self.db.kv_set(key, str(int(self.db.kv_get(key, "0") or 0) + n))

    # ------------------------------------------------------------- back-off
    def cooling_down(self) -> str | None:
        state = json.loads(self.db.kv_get(f"source:{self.name}:backoff", "{}") or "{}")
        until = state.get("until")
        if until and datetime.fromisoformat(until) > datetime.now(timezone.utc):
            return f"backing off until {until} ({state.get('why', '')})"
        return None

    def back_off(self, why: str) -> None:
        """Exponential: 30 min, 1 h, 2 h ... capped at 24 h. Reset after a success."""
        state = json.loads(self.db.kv_get(f"source:{self.name}:backoff", "{}") or "{}")
        minutes = min(int(state.get("minutes", 15)) * 2, 24 * 60)
        until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        self.db.kv_set(f"source:{self.name}:backoff", json.dumps({"until": until, "minutes": minutes, "why": why[:200]}))
        log.warning("%s: %s; backing off %d min", self.name, why, minutes)

    def reset_backoff(self) -> None:
        if self.db.kv_get(f"source:{self.name}:backoff"):
            self.db.kv_set(f"source:{self.name}:backoff", "{}")


def build_adapters(cfg: Config, db: DB, fetcher, geo) -> list[SourceAdapter]:
    from app.sources.news_rss import NewsRssAdapter
    from app.sources.osm import OsmAdapter

    return [OsmAdapter(cfg, db, fetcher, geo), NewsRssAdapter(cfg, db, fetcher, geo)]
