"""Free web search: DuckDuckGo (via the `ddgs` library, no key) and optionally
Google Programmable Search JSON API (free tier: 100 queries/day).

We never scrape google.com/search: Google's robots.txt disallows it.
"""
from __future__ import annotations

import logging
import random
import time

import requests

from app.config import Config

log = logging.getLogger("search")


class SearchRateLimited(Exception):
    pass


class SearchClient:
    def __init__(self, cfg: Config):
        r = cfg.settings["research"]
        self.sources: list[str] = r.get("sources", ["duckduckgo"])
        self.gap = tuple(r.get("seconds_between_queries", [6, 12]))
        self.cse_key = cfg.env("GOOGLE_CSE_KEY")
        self.cse_cx = cfg.env("GOOGLE_CSE_CX")
        self._last = 0.0

    def _pace(self) -> None:
        wait = self._last + random.uniform(*self.gap) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def search(self, query: str, max_results: int = 10) -> list[dict]:
        """Returns [{title, url, snippet, engine}]."""
        out: list[dict] = []
        if "duckduckgo" in self.sources:
            out += self._ddg(query, max_results)
        if "google_cse" in self.sources and self.cse_key and self.cse_cx:
            out += self._google_cse(query, max_results)
        seen, uniq = set(), []
        for r in out:
            if r["url"] and r["url"] not in seen:
                seen.add(r["url"])
                uniq.append(r)
        return uniq

    def _ddg(self, query: str, n: int) -> list[dict]:
        try:
            from ddgs import DDGS
        except ImportError:  # older package name
            from duckduckgo_search import DDGS  # type: ignore
        self._pace()
        for attempt in range(2):
            try:
                try:
                    rows = DDGS().text(query, region="in-en", safesearch="moderate", max_results=n, backend="duckduckgo")
                except (TypeError, ValueError):
                    rows = DDGS().text(query, region="in-en", safesearch="moderate", max_results=n)
                return [
                    {"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""),
                     "snippet": r.get("body", ""), "engine": "duckduckgo"}
                    for r in rows or []
                ]
            except Exception as e:
                msg = str(e).lower()
                if "ratelimit" in msg or "202" in msg or "429" in msg:
                    if attempt == 0:
                        log.warning("DuckDuckGo rate-limited; waiting 60s")
                        time.sleep(60)
                        continue
                    raise SearchRateLimited(str(e)) from e
                log.warning("DuckDuckGo search failed for %r: %s", query, e)
                return []
        return []

    def _google_cse(self, query: str, n: int) -> list[dict]:
        self._pace()
        try:
            r = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": self.cse_key, "cx": self.cse_cx, "q": query, "num": min(n, 10), "gl": "in"},
                timeout=20,
            )
            if r.status_code == 429:
                raise SearchRateLimited("Google CSE daily quota reached")
            r.raise_for_status()
            return [
                {"title": i.get("title", ""), "url": i.get("link", ""), "snippet": i.get("snippet", ""), "engine": "google_cse"}
                for i in r.json().get("items", [])
            ]
        except SearchRateLimited:
            raise
        except Exception as e:
            log.warning("Google CSE failed for %r: %s", query, e)
            return []
