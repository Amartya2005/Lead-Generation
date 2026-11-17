"""Startup / SME news discovery from public RSS feeds (Inc42, YourStory, Entrackr ...).

Articles are never leads. From each item's headline we extract the company it is about
("AI CRM startup Dreamteam raises $53 Mn" -> "Dreamteam"), then the Research Agent finds
its official website (a link inside the item whose domain carries the name, else one
strict DuckDuckGo lookup within a daily budget). Geographic eligibility is decided by the
existing ICP: items that are clearly about a foreign company are dropped here, and every
saved lead is located from its own website by Enrichment and scored by Qualification.

TERMS GATE: a feed is fetched only if its config says `terms: permitted`. robots.txt is
checked on every request (PoliteFetcher). As verified on 2026-11-17, Inc42's and Entrackr's
terms prohibit automated collection without written consent, and YourStory's terms could
not be read (bot challenge), so all three ship as non-runnable.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Iterator

from bs4 import BeautifulSoup

from app.sources import Candidate, SourceAdapter
from app.tools.geo import FOREIGN_RE
from app.tools.http import FetchError, RobotsDisallowed

log = logging.getLogger("sources.news_rss")

PREFIX = re.compile(r"^(?:exclusive|breaking|funding alert|report|update|scoop)\s*[:|\-–]\s*", re.I)
EVENT = (r"raises|raised|bags|secures|closes|lands|nets|launches|unveils|acquires|posts|clocks|expands|"
         r"opens|enters|partners with|rolls out|introduces|onboards|ropes in|picks up")
HOW = re.compile(r"^how\s+(?P<name>.+?)\s+(?:is|are|was|built|builds|became|grew|scaled|carved|turned|won|"
                 r"makes|helps|is helping|went|bootstrapped|cracked)\b", re.I)
ACTION = re.compile(rf"^(?P<pre>.+?)\s+(?:{EVENT})\b", re.I)
# "Equirus fund leads Rs 10 Cr seed round in deep-tech startup CountAI"
INVESTED_IN = re.compile(r"\b(?:round|investment|stake|funding)\s+in\s+.*?\b(?:startup|company|platform|brand|firm)\s+"
                         r"(?P<name>[A-Z0-9][^\s,]*(?:\s+[A-Z0-9][^\s,]*){0,3})\s*$", re.I)
# "AI CRM startup Dreamteam", "Wealthtech startup finny.club", "D2C brand Foo"
DESCRIPTOR = re.compile(  # greedy prefix: the LAST descriptor wins ("agritech startup X" -> "X")
    r"^.*\b(?:startup|start-up|company|brand|platform|firm|maker|marketplace|provider|player|unicorn|venture|label|"
    r"chain|app|fintech|edtech|healthtech|agritech|saas|msme|sme|manufacturer|exporter)\s+(?P<name>\S.*)$", re.I)
INVESTOR_NAME = re.compile(r"\b(?:capital|ventures?|venture partners|partners|fund|funds|investments?|vc|advisors|"
                           r"angels?|holdings|family office)$", re.I)
FUND_NEWS = re.compile(r"\b(?:fund|first close|final close|aum|corpus)\b", re.I)
NOT_COMPANIES = {
    "india", "indian", "govt", "government", "centre", "center", "sebi", "rbi", "nse", "bse", "budget", "ministry",
    "court", "hc", "sc", "supreme court", "dpiit", "meity", "cert-in", "niti aayog", "cabinet", "parliament", "startups",
    "msmes", "smes", "indian startups", "indian msmes", "investors", "vcs", "founders", "this", "these", "who", "why",
}


def company_from_headline(title: str) -> str | None:
    """Name of the single company a headline is about, or None (people, policy, roundups)."""
    t = PREFIX.sub("", " ".join((title or "").split()))
    m = HOW.match(t) or INVESTED_IN.search(t) or ACTION.match(t)
    if not m or (m.re is ACTION and FUND_NEWS.search(t)):  # an investor raising/launching a fund: not ICP
        return None
    name = (m.group("name") if "name" in m.groupdict() and m.group("name") else m.groupdict().get("pre") or "").strip()
    d = DESCRIPTOR.search(name)
    if d:
        name = d.group("name").strip()
    name = re.sub(r"^(?:the|a|an)\s+", "", name, flags=re.I).strip(" ,.:;'\"‘’“”")
    name = re.sub(r"\s+(?:parent|arm|unit|subsidiary)$", "", name, flags=re.I)
    if INVESTOR_NAME.search(name):
        return None
    words = name.split()
    if not 1 <= len(words) <= 5 or len(name) > 60:
        return None
    if re.search(r",|\band\b|&|[’']s\b|\bvs\b", name, re.I):   # several entities, or "X's arm ..."
        return None
    if name.lower() in NOT_COMPANIES or re.search(r"-based\b", name, re.I):
        return None
    if not (name[0].isupper() or name[0].isdigit() or re.search(r"\.[a-z]{2,}$", name)):  # "finny.club" is fine
        return None
    return name


class NewsRssAdapter(SourceAdapter):
    name = "news_rss"

    def feeds(self) -> list[dict]:
        return [f for f in (self.conf.get("feeds") or []) if f.get("url") and f.get("name")]

    @staticmethod
    def runnable(feed: dict) -> bool:
        return feed.get("terms") == "permitted"

    def status(self) -> dict:
        s = super().status()
        s["feeds"] = {f["name"]: f.get("terms", "unverified") for f in self.feeds()}
        s["resolutions_left_today"] = self.resolutions_left()
        return s

    def _items(self, xml_text: str) -> list[ET.Element]:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
        items = root.findall("./channel/item")
        return items or root.findall("{http://www.w3.org/2005/Atom}entry")

    @staticmethod
    def _text(item: ET.Element, *tags: str) -> str:
        for t in tags:
            el = item.find(t)
            if el is not None and (el.text or el.get("href")):
                return (el.text or el.get("href") or "").strip()
        return ""

    def discover(self, deadline: float | None = None) -> Iterator[Candidate]:
        if not self.enabled:
            return
        min_gap = float(self.conf.get("min_hours_between_fetches", 12))
        per_feed = int(self.conf.get("max_items_per_feed", 30))
        for feed in self.feeds():
            if not self.runnable(feed):
                log.info("news_rss: %s skipped (terms: %s)", feed["name"], feed.get("terms", "unverified"))
                continue
            if deadline is not None and time.monotonic() > deadline:
                return
            key = f"rss:{feed['name']}"
            last = self.last_run(key)
            if last and (time.time() - _ts(last)) < min_gap * 3600:
                continue
            self.db.record_query(key, self.name, -1)
            try:
                page = self.fetcher.fetch(feed["url"], accept=("xml", "rss", "atom"), retries=1,
                                          cache_hours=max(min_gap - 0.5, 0))
                items = self._items(page.html)
            except RobotsDisallowed:
                log.warning("news_rss: robots.txt disallows %s", feed["url"])
                continue
            except (FetchError, ET.ParseError) as e:
                log.warning("news_rss: %s failed: %s", feed["name"], e)
                continue
            self.db.record_query(key, self.name, len(items))
            for item in items[:per_feed]:
                c = self.candidate(feed, item)
                if c:
                    yield c

    def candidate(self, feed: dict, item: ET.Element) -> Candidate | None:
        title = self._text(item, "title", "{http://www.w3.org/2005/Atom}title")
        name = company_from_headline(title)
        if not name:
            return None
        link = self._text(item, "link", "{http://www.w3.org/2005/Atom}link")
        body = " ".join(self._text(item, t) for t in (
            "description", "{http://purl.org/rss/1.0/modules/content/}encoded", "{http://www.w3.org/2005/Atom}summary"))
        soup = BeautifulSoup(body, "lxml")
        text = " ".join(soup.get_text(" ").split())[:800]
        loc = self.geo.detect(f"{title} {text}")
        if FOREIGN_RE.search(f"{title} {text}") and not (loc["city"] or loc["state"] or "india" in text.lower()):
            return None  # clearly about a company outside India: not ICP
        links = [a["href"] for a in soup.find_all("a", href=True) if a["href"].startswith("http")]
        pub = self._text(item, "pubDate", "{http://www.w3.org/2005/Atom}updated")
        return Candidate(company=name, url=None, source=f"news:{feed['name']}", source_url=link or feed["url"],
                         evidence=f"{title} ({feed['name']}{', ' + pub if pub else ''})"[:500],
                         city_hint=loc["city"], query=f"rss:{feed['name']}", links=links[:20], resolve=True)


def _ts(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()
