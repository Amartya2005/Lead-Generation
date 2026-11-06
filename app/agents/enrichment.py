"""Enrichment Agent: visits each lead's own website (robots.txt respected) and
extracts website, location, contact page/email, LinkedIn, size signals and tech clues.

LinkedIn is only taken from links on the company's own site. We never crawl LinkedIn.
"""
from __future__ import annotations

import logging

from app.config import Config
from app.db import DB
from app.tools.domains import registered_domain
from app.tools.extract import extract, merge
from app.tools.geo import Geo
from app.tools.http import FetchError, PoliteFetcher, RobotsDisallowed

log = logging.getLogger("enrichment")

# Stored in the DB (the page text is kept short; it's only used for scoring).
KEEP = ("title", "description", "tech", "generator", "jquery_version", "copyright_year", "has_viewport", "https",
        "layout_tables", "online", "google_forms", "spreadsheet_links", "manual_phrases", "growth_phrases",
        "has_careers", "employees", "founded", "listed_company", "whatsapp_links", "whatsapp_mentions",
        "socials", "pages", "word_count", "rendered")


class EnrichmentAgent:
    name = "enrichment"

    def __init__(self, cfg: Config, db: DB, fetcher: PoliteFetcher, geo: Geo):
        self.cfg, self.db, self.fetcher, self.geo = cfg, db, fetcher, geo
        self.max_pages = int(cfg.settings["crawl"].get("max_pages_per_site", 3))

    def _fetch_home(self, website: str):
        """Try the URL as given, then the https/http alternative."""
        candidates = [website]
        if website.startswith("https://"):
            candidates.append("http://" + website[len("https://"):])
        last_err: Exception | None = None
        for url in candidates:
            try:
                return self.fetcher.fetch(url)
            except RobotsDisallowed:
                raise
            except FetchError as e:
                last_err = e
        raise FetchError(str(last_err))

    def enrich_one(self, lead) -> str:
        lead_id = lead["id"]
        domain = lead["domain"] or registered_domain(lead["website"])
        try:
            home = self._fetch_home(lead["website"])
        except RobotsDisallowed as e:
            self.db.update_lead(lead_id, status="blocked_robots", error=str(e))
            self.db.log(self.name, "blocked_robots", lead_id, str(e))
            return "blocked_robots"
        except FetchError as e:
            self.db.update_lead(lead_id, status="unreachable", error=str(e)[:300])
            self.db.log(self.name, "unreachable", lead_id, str(e)[:300])
            return "unreachable"

        pages = [extract(home.html, home.final_url, domain)]
        pages[0]["rendered"] = home.rendered
        for kind in ("contact", "about", "careers"):
            if len(pages) >= self.max_pages:
                break
            sub = pages[0]["pages"].get(kind)
            if not sub:
                continue
            try:
                p = self.fetcher.fetch(sub)
                pages.append(extract(p.html, p.final_url, domain))
            except (RobotsDisallowed, FetchError) as e:
                log.debug("skip %s page for %s: %s", kind, domain, e)

        sig = merge(pages)
        loc = self.geo.detect(sig["text"] + " " + (lead["snippet"] or ""), lead["city_hint"])
        company = lead["company"]
        if (not company or company == domain) and sig["title"]:
            company = sig["title"].split("|")[0].split(" - ")[0].strip()[:120] or domain

        stored = {k: sig.get(k) for k in KEEP}
        stored["text_excerpt"] = sig["text"][:1500]
        stored["geo"] = loc
        stored["final_url"] = home.final_url

        self.db.update_lead(
            lead_id,
            company=company,
            website=home.final_url,
            location=loc["location"], city=loc["city"], state=loc["state"], tier=loc["tier"],
            email=sig["emails"][0] if sig["emails"] else None,
            all_emails=", ".join(sig["emails"][:5]),
            phone=", ".join(sig["phones"][:2]),
            whatsapp=int(sig["whatsapp_links"] > 0 or sig["whatsapp_mentions"] >= 2),
            linkedin=sig["linkedin"],
            signals=stored,
            status="enriched",
            error=None,
        )
        self.db.log(self.name, "enriched", lead_id,
                    {"email": bool(sig["emails"]), "location": loc["location"], "tech": sig["tech"],
                     "pages": len(pages), "rendered": home.rendered})
        return "enriched"

    def run(self, limit: int = 50) -> dict:
        counts: dict[str, int] = {}
        for lead in self.db.leads_by_status("discovered", limit=limit):
            if not lead["website"]:
                continue
            res = self.enrich_one(lead)
            counts[res] = counts.get(res, 0) + 1
            log.info("Enriched %-35s -> %s", lead["company"][:35], res)
        return counts
