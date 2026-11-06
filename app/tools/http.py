"""Polite HTTP fetcher.

* Checks robots.txt before every request (RFC 9309 semantics: 4xx = allow all,
  5xx / unreachable = disallow all) for both the requested and the final host.
* Per-host minimum delay; robots.txt Crawl-delay is honoured when larger.
* Backs off on 429/503 using Retry-After.
* Caches pages in SQLite so re-runs don't re-crawl.
* Optional Playwright render for JavaScript-only pages (same robots rules apply).
"""
from __future__ import annotations

import logging
import random
import re
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from app.config import Config
from app.db import DB

log = logging.getLogger("fetcher")


class RobotsDisallowed(Exception):
    pass


class FetchError(Exception):
    pass


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    html: str
    content_type: str
    from_cache: bool = False
    rendered: bool = False


_JS_HINTS = re.compile(r"enable javascript|you need to enable js|<div id=\"(root|app|__next)\">\s*</div>", re.I)


def looks_js_only(html: str) -> bool:
    body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", body)
    return len(" ".join(text.split())) < 300 or bool(_JS_HINTS.search(html))


class PoliteFetcher:
    def __init__(self, cfg: Config, db: DB):
        c = cfg.settings["crawl"]
        self.db = db
        self.ua: str = c["user_agent"]
        self.delay = float(c.get("per_host_delay_seconds", 6))
        self.timeout = float(c.get("timeout_seconds", 20))
        self.cache_hours = float(c.get("cache_hours", 72))
        self.use_playwright = bool(c.get("use_playwright_fallback", True))
        self.max_chars = int(c.get("max_body_chars", 600_000))
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": self.ua, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-IN,en;q=0.9"}
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_hit: dict[str, float] = {}
        self._pw = None
        self._browser = None

    # ----------------------------------------------------------- robots.txt
    @staticmethod
    def _origin(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}".lower()

    def robots(self, url: str) -> urllib.robotparser.RobotFileParser:
        origin = self._origin(url)
        if origin in self._robots:
            return self._robots[origin]
        rp = urllib.robotparser.RobotFileParser()
        try:
            self._wait(origin, 0)
            r = self.session.get(origin + "/robots.txt", timeout=self.timeout, allow_redirects=True)
            if r.status_code >= 500:
                rp.parse(["User-agent: *", "Disallow: /"])
            elif r.status_code >= 400:
                rp.parse([])  # no robots.txt -> everything allowed
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException as e:
            log.debug("robots.txt unreachable for %s (%s); treating as disallow", origin, e)
            rp.parse(["User-agent: *", "Disallow: /"])
        self._robots[origin] = rp
        return rp

    def allowed(self, url: str) -> bool:
        return self.robots(url).can_fetch(self.ua, url)

    # ------------------------------------------------------------ throttling
    def _wait(self, origin: str, crawl_delay: float | None) -> None:
        host = urlparse(origin).netloc
        gap = max(self.delay, float(crawl_delay or 0))
        last = self._last_hit.get(host)
        if last is not None:
            sleep_for = last + gap - time.monotonic()
            if sleep_for > 0:
                time.sleep(sleep_for + random.uniform(0, 1.5))
        self._last_hit[host] = time.monotonic()

    # ----------------------------------------------------------------- fetch
    def fetch(self, url: str, render: bool | None = None) -> Page:
        cached = self.db.cache_get(url, self.cache_hours)
        if cached and cached["body"] is not None:
            return Page(url, cached["final_url"], cached["status"], cached["body"], cached["content_type"] or "",
                        from_cache=True, rendered=bool(cached["rendered"]))

        rp = self.robots(url)
        if not rp.can_fetch(self.ua, url):
            raise RobotsDisallowed(url)

        resp = None
        for attempt in range(3):
            self._wait(self._origin(url), rp.crawl_delay(self.ua))
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException as e:
                if attempt == 2:
                    raise FetchError(f"{url}: {e}") from e
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code in (429, 503):
                retry_after = resp.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else 15 * (2 ** attempt)
                log.info("%s returned %s; backing off %ss", url, resp.status_code, min(wait, 120))
                time.sleep(min(wait, 120))
                continue
            break
        if resp is None:
            raise FetchError(f"{url}: no response")

        final_url = resp.url
        if self._origin(final_url) != self._origin(url) and not self.allowed(final_url):
            raise RobotsDisallowed(final_url)

        ctype = resp.headers.get("Content-Type", "")
        if resp.status_code >= 400:
            raise FetchError(f"{url}: HTTP {resp.status_code}")
        if "html" not in ctype.lower():
            raise FetchError(f"{url}: not HTML ({ctype})")

        html = resp.text[: self.max_chars]
        rendered = False
        if (render or (render is None and self.use_playwright)) and looks_js_only(html):
            rendered_html = self._render(final_url)
            if rendered_html:
                html, rendered = rendered_html[: self.max_chars], True

        self.db.cache_put(url, resp.status_code, final_url, ctype, html, rendered)
        return Page(url, final_url, resp.status_code, html, ctype, rendered=rendered)

    # ------------------------------------------------------------ playwright
    def _render(self, url: str) -> str | None:
        try:
            if self._browser is None:
                from playwright.sync_api import sync_playwright

                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch(headless=True)
            ctx = self._browser.new_context(user_agent=self.ua, locale="en-IN")
            page = ctx.new_page()
            # Block heavy assets: we only need the DOM.
            page.route("**/*", lambda r: r.abort() if r.request.resource_type in {"image", "media", "font"} else r.continue_())
            page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
            page.wait_for_timeout(2500)
            html = page.content()
            ctx.close()
            return html
        except Exception as e:  # Playwright not installed, browser missing, timeout...
            log.debug("Playwright render failed for %s: %s", url, e)
            return None

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser = self._pw = None
