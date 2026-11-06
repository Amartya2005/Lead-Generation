"""Serverless scheduler step ("tick").

On Vercel there are no long-running processes, so a scheduler (GitHub Actions every
~10 minutes, plus a daily Vercel Cron) calls GET /api/tick. Each tick does a bounded
amount of work and stops before the function time limit:

  1. mark leads stuck in 'sending' as failed (never re-sent)
  2. send at most ONE approved email (window, daily limit, suppression and the random
     gap between emails are enforced from the database)
  3. inbox sync, every 2 hours
  4. pipeline work: qualify -> match -> draft what's pending, enrich a few sites,
     carry those through to drafts, then run one research query

Ticks never overlap (database lease) and are spaced at least `min_interval_seconds`
apart, so calling the endpoint more often cannot speed up sending or crawling.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import Config
from app.db import DB, AlreadyRunning

log = logging.getLogger("tick")


def _ist_day_start(cfg: Config) -> str:
    tz = ZoneInfo(cfg.settings["sending"].get("send_window", {}).get("timezone", "Asia/Kolkata"))
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def _due(db: DB, key: str, every_seconds: int) -> bool:
    last = db.kv_get(key)
    return not last or datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(seconds=every_seconds)


def _stamp(db: DB, key: str) -> None:
    db.kv_set(key, datetime.now(timezone.utc).isoformat(timespec="seconds"))


def tick(cfg: Config, db: DB, budget_seconds: float | None = None, force: bool = False) -> dict:
    """Run one bounded step. Returns counts only (no lead data: safe for public CI logs)."""
    t = cfg.settings.get("tick", {})
    budget = float(budget_seconds or os.getenv("TICK_BUDGET_SECONDS") or t.get("budget_seconds", 240))
    min_interval = int(t.get("min_interval_seconds", 240))
    deadline = time.monotonic() + budget
    left = lambda: deadline - time.monotonic()  # noqa: E731

    if not force and not _due(db, "tick_last", min_interval):
        return {"skipped": "too soon after the previous tick"}
    try:
        with db.lock("tick", ttl_seconds=int(budget) + 120):
            _stamp(db, "tick_last")
            return _work(cfg, db, left, t)
    except AlreadyRunning:
        return {"skipped": "a tick is already running"}


def _work(cfg: Config, db: DB, left, t: dict) -> dict:
    from app.agents.sender import SenderAgent
    from app.graph import Agents

    out: dict = {}
    if db.kv_get("diag_requested") == "1":  # set by an admin/operator to debug mail connectivity
        import json

        from app.tools.diagnose import mail_report

        db.kv_set("diag_requested", "0")
        db.kv_set("diag_result", json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                              "report": mail_report(cfg)}))
        out["diagnostics"] = "stored in kv.diag_result"
    sender = SenderAgent(cfg, db)
    out["recovered"] = sender.recover_stuck()

    # 1) at most one email
    out["send"] = sender.send_one()

    # 2) inbox: replies + bounces
    if _due(db, "inbox_last", int(t.get("inbox_every_seconds", 7200))) and left() > 60:
        from app.tools.imap_sync import sync_inbox

        try:
            out["inbox"] = sync_inbox(cfg, db, days=14)
        except Exception as e:  # never let IMAP problems stop the pipeline
            log.warning("inbox sync failed: %s", e)
            out["inbox"] = {"error": type(e).__name__}
        _stamp(db, "inbox_last")

    if _due(db, "cache_pruned", 86400):
        db.prune_cache(float(cfg.settings["crawl"].get("cache_hours", 72)))
        _stamp(db, "cache_pruned")

    # 3) pipeline, downstream first
    a = Agents(cfg, db)
    reserve = float(t.get("reserve_seconds", 45))  # stop starting new work this close to the limit
    try:
        counts = {"drafted": 0, "matched": 0, "qualified": 0, "enriched": 0, "new_leads": 0}
        qualifier, matcher, writer, enricher = a.qualification(), a.matching(), a.email(), a.enrichment()

        def downstream() -> None:
            """qualify -> match -> draft, in order, so a lead can go all the way in one pass."""
            for status, fn, key in (("enriched", qualifier.qualify_one, "qualified"),
                                    ("qualified", matcher.match_one, "matched"),
                                    ("matched", writer.draft_one, "drafted")):
                for lead in db.leads_by_status(status, limit=30):
                    if left() < reserve:
                        return
                    fn(lead)
                    counts[key] += 1

        downstream()                      # finish what's already in the pipeline
        for lead in db.leads_by_status("discovered", limit=15):
            if left() < reserve + 30:     # a site visit can take ~30 s
                break
            if lead["website"]:
                enricher.enrich_one(lead)
                counts["enriched"] += 1
        downstream()                      # carry today's new leads through to drafts

        # One research query per tick, until today's quota is used.
        r = cfg.settings["research"]
        day = _ist_day_start(cfg)
        queries_today = db.queries_since(day)
        new_today = db.scalar("SELECT count(*) FROM leads WHERE created_at>=? AND source<>'manual'", (day,))
        pending = db.scalar("SELECT count(*) FROM leads WHERE status='discovered'")
        if (left() > reserve + 40 and queries_today < int(r.get("queries_per_run", 8))
                and new_today < int(r.get("max_new_leads_per_run", 30)) and pending < 20):
            agent = a.research()
            queries = agent.build_queries()[:1]
            if queries:
                counts["new_leads"] = agent.run_search(int(r.get("max_new_leads_per_run", 30)) - new_today, queries,
                                                       deadline=time.monotonic() + left() - reserve)
        out["pipeline"] = counts
    finally:
        a.close()
    out["seconds_left"] = round(left())
    return out
