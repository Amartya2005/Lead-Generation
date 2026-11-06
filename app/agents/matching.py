"""Matching Agent: maps a lead's needs to the single most relevant portfolio project.

Deterministic on purpose: a project can only be matched if its declared
`capabilities` cover the need, so the email can never claim work that isn't there.
Client work is preferred over our own/personal sites as the example.
"""
from __future__ import annotations

import logging

from app.config import Config
from app.db import DB, loads

log = logging.getLogger("matching")
KIND_BONUS = {"client": 1.0, "own_company": 0.3, "personal": 0.0}
NEED_ALIASES = {"registration": ["registration", "event", "community"], "custom_app": ["custom_app"],
                "website": ["website"], "automation": ["automation"], "crm": ["crm"], "erp": ["erp"]}


def best_project(cfg: Config, needs: list[str], industry: str | None) -> tuple[dict | None, str]:
    best, best_score, best_hits = None, -1.0, []
    for proj in cfg.portfolio:
        caps = set(proj["capabilities"])
        score, hits = 0.0, []
        for rank, need in enumerate(needs):
            weight = 3.0 - rank  # first need matters most
            if caps & set(NEED_ALIASES.get(need, [need])):
                score += weight
                hits.append(need)
        if not hits:
            continue
        if industry and industry in proj.get("industries", []):
            score += 1.5
            hits.append(f"industry:{industry}")
        score += KIND_BONUS.get(proj.get("kind", "client"), 0)
        if score > best_score:
            best, best_score, best_hits = proj, score, hits
    if not best:
        return None, "no portfolio project covers these needs"
    return best, f"covers {', '.join(best_hits)} (score {best_score:g})"


class MatchingAgent:
    name = "matching"

    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db

    def run(self, limit: int = 100) -> dict:
        counts = {"matched": 0, "unmatched": 0}
        for lead in self.db.leads_by_status("qualified", limit=limit):
            needs = loads(lead["needs"], []) or ["website"]
            proj, why = best_project(self.cfg, needs, lead["industry"])
            if not proj:
                self.db.update_lead(lead["id"], status="discarded", match_reason=why)
                self.db.log(self.name, "unmatched", lead["id"], why)
                counts["unmatched"] += 1
                continue
            self.db.update_lead(lead["id"], matched_project=proj["id"], match_reason=why, status="matched")
            self.db.log(self.name, "matched", lead["id"], {"project": proj["id"], "why": why})
            counts["matched"] += 1
            log.info("Matched %-35s -> %s (%s)", lead["company"][:35], proj["domain"], why)
        return counts
