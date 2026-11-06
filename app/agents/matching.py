"""Matching Agent: assigns a team member to each lead and maps the lead's needs to
the single most relevant portfolio project that member may cite.

Deterministic on purpose: a project can only be matched if its declared
`capabilities` cover the need, so the email can never claim work that isn't there.
Client work is preferred over our own/personal sites as the example, and a
personal portfolio (`owner:` in portfolio.yaml) is only offered to its owner.
"""
from __future__ import annotations

import logging

from app.config import Config
from app.db import DB, loads

log = logging.getLogger("matching")
KIND_BONUS = {"client": 1.0, "own_company": 0.3, "personal": 0.0}
NEED_ALIASES = {"registration": ["registration", "event", "community"], "custom_app": ["custom_app"],
                "website": ["website"], "automation": ["automation"], "crm": ["crm"], "erp": ["erp"]}


def best_project(cfg: Config, needs: list[str], industry: str | None,
                 member_id: str | None = None) -> tuple[dict | None, str]:
    best, best_score, best_hits = None, -1.0, []
    for proj in cfg.portfolio:
        if not cfg.project_allowed_for(proj, member_id):
            continue
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

    def __init__(self, cfg: Config, db: DB, sender_override: str | None = None):
        self.cfg, self.db = cfg, db
        if sender_override and not cfg.member(sender_override):
            raise ValueError(f"unknown team member '{sender_override}' (see config/team.yaml)")
        self.sender_override = sender_override
        self._assigned = db.assigned_counts()

    def pick_member(self) -> str:
        if self.sender_override:
            return self.sender_override
        active = self.cfg.active_members or self.cfg.members
        if self.cfg.team.get("assignment") == "round_robin" and len(active) > 1:
            choice = min(active, key=lambda m: self._assigned.get(m["id"], 0))["id"]
        else:
            choice = self.cfg.member(None)["id"]
        self._assigned[choice] = self._assigned.get(choice, 0) + 1
        return choice

    def match_one(self, lead, member_id: str | None = None) -> str:
        member_id = member_id or self.pick_member()
        needs = loads(lead["needs"], []) or ["website"]
        proj, why = best_project(self.cfg, needs, lead["industry"], member_id)
        if not proj:
            self.db.update_lead(lead["id"], status="discarded", match_reason=why, sender_id=member_id)
            self.db.log(self.name, "unmatched", lead["id"], why)
            return "unmatched"
        self.db.update_lead(lead["id"], matched_project=proj["id"], match_reason=why, sender_id=member_id,
                            status="matched")
        self.db.log(self.name, "matched", lead["id"], {"project": proj["id"], "sender": member_id, "why": why})
        log.info("Matched %-35s -> %s, signed by %s (%s)", lead["company"][:35], proj["domain"], member_id, why)
        return "matched"

    def run(self, limit: int = 100) -> dict:
        counts = {"matched": 0, "unmatched": 0}
        for lead in self.db.leads_by_status("qualified", limit=limit):
            counts[self.match_one(lead)] += 1
        return counts
