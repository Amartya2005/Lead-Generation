"""Serverless scheduler step ("tick").

On Vercel there are no long-running processes, so a scheduler (GitHub Actions every
~10 minutes, plus a daily Vercel Cron) calls GET /api/tick. Each tick does a bounded
amount of work and stops before the function time limit:

  1. mark leads stuck in 'sending' as failed (never re-sent)
  2. send at most ONE approved email (window, daily limit, suppression and the random
     gap between emails are enforced from the database). SKIPPED while
     `sending.enabled` is false (lead-generation-only mode, the default).
  3. inbox sync, every 2 hours (also skipped in lead-generation-only mode: nothing was sent)
  4. pipeline work: qualify -> match -> draft what's pending, enrich sites, carry those
     through to drafts, then discover: several search queries plus the enabled source
     adapters. There is no daily lead or query ceiling; each tick is bounded by its time
     budget, the per-tick budgets in settings.yaml (tick.discovery / tick.processing),
     provider back-off, and a backlog threshold that pauses discovery until the pipeline
     catches up (tick.backpressure).

Ticks never overlap (database lease) and are spaced at least `min_interval_seconds`
apart, so calling the endpoint more often cannot speed up sending or crawling.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

    # 1) at most one email (never in lead-generation-only mode)
    out["send"] = sender.send_one() if cfg.sending_enabled else {"skipped": "sending disabled"}

    # 2) inbox: replies + bounces
    if cfg.sending_enabled and _due(db, "inbox_last", int(t.get("inbox_every_seconds", 7200))) and left() > 60:
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

    # 3) pipeline: drain the backlog while discovering (no daily ceiling; per-tick budgets only).
    # Shares the "run" lock with `python main.py run`, so the two never process the same leads.
    a = Agents(cfg, db)
    try:
        with db.lock("run", ttl_seconds=int(float(t.get("budget_seconds", 240))) + 120):
            out.update(_pipeline(cfg, db, a, left, t, float(t.get("reserve_seconds", 45))))
    except AlreadyRunning:
        out["pipeline"] = {"skipped": "a pipeline run is in progress"}
    finally:
        a.close()
    out["seconds_left"] = round(left())
    return out


def _discovery_gate(db: DB, t: dict) -> str | None:
    """Backpressure with hysteresis. Returns why discovery is paused, or None to discover."""
    bp = t.get("backpressure") or {}
    pause_at, resume_below = int(bp.get("pause_discovery_at", 120)), int(bp.get("resume_discovery_below", 60))
    backlog = db.scalar("SELECT count(*) FROM leads WHERE status IN ('discovered','enriched')")
    paused = db.kv_get("discovery_paused") == "1"
    if paused and backlog < resume_below:
        db.kv_set("discovery_paused", "0")
        log.info("Backlog %d < %d: discovery resumed", backlog, resume_below)
        paused = False
    elif not paused and backlog >= pause_at:
        db.kv_set("discovery_paused", "1")
        log.info("Backlog %d >= %d: discovery paused until the pipeline catches up", backlog, pause_at)
        paused = True
    return f"paused: backlog {backlog} (resumes below {resume_below})" if paused else None


def _discover(cfg: Config, db: DB, a, deadline: float, d: dict) -> tuple[int, dict]:
    """Search queries for this tick, then the enabled source adapters, within one lead budget."""
    agent = a.research()
    agent.search.rate_limit_wait = 0  # back off across ticks instead of sleeping in this one
    budget = int(d.get("max_new_leads_per_tick", 50))
    new = 0
    if int(d.get("queries_per_tick", 4)) > 0:
        queries = agent.build_queries(int(d.get("queries_per_tick", 4)))
        if queries:
            new = agent.run_search(budget, queries, deadline=deadline)
    sources: dict = {}
    adapters = cfg.settings["research"].get("adapters") or {}
    if (d.get("run_sources", True) and any((v or {}).get("enabled") is True for v in adapters.values())
            and new < budget and time.monotonic() < deadline):
        res = agent.run_sources(budget - new, deadline=deadline)
        sources = {k: v.get("added", 0) for k, v in res.items() if "skipped" not in v}
    return new, sources


def _enrich(cfg: Config, db: DB, left, reserve: float, p: dict) -> int:
    """Enrich 'discovered' leads with a small thread pool. Each worker has its own fetcher;
    leads are distinct companies (unique domains), so per-host politeness is unchanged.
    Every lead is handed to exactly one worker. Returns the number of sites visited."""
    from app.graph import Agents

    workers, limit = max(1, int(p.get("enrich_workers", 3))), int(p.get("max_enrich_per_tick", 40))
    local, made, made_lock = threading.local(), [], threading.Lock()

    def work(lead) -> bool:
        if left() < reserve + 30:     # a site visit can take ~30 s
            return False
        if not hasattr(local, "agents"):
            local.agents = Agents(cfg, db)
            with made_lock:
                made.append(local.agents)
        try:
            local.agents.enrichment().enrich_one(lead)
            return True
        except Exception as e:  # one bad site must not stop the others
            log.exception("enrichment failed for lead %s: %s", lead["id"], e)
            return False

    seen: set[int] = set()
    visited = 0
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="enrich") as pool:
            while len(seen) < limit and left() > reserve + 30:
                batch = [l for l in db.leads_by_status("discovered", limit=limit + len(seen)) if l["id"] not in seen]
                batch = batch[: min(workers * 2, limit - len(seen))]
                if not batch:
                    break
                seen.update(l["id"] for l in batch)
                visited += sum(pool.map(work, [l for l in batch if l["website"]]))
    finally:
        for agents in made:
            agents.close()
    return visited


def _pipeline(cfg: Config, db: DB, a, left, t: dict, reserve: float) -> dict:
    p, d = t.get("processing") or {}, t.get("discovery") or {}
    counts = {"drafted": 0, "matched": 0, "qualified": 0, "enriched": 0, "new_leads": 0}
    qualifier, matcher, writer = a.qualification(), a.matching(), a.email()

    def downstream() -> None:
        """qualify -> match -> draft, in order, so a lead can go all the way in one pass."""
        # Matching is cheap (no network), and leads waiting for portfolio confirmation stay
        # 'qualified', so look further down that queue than the others.
        for status, fn, key, n in (("enriched", qualifier.qualify_one, "qualified", int(p.get("qualify_batch", 30))),
                                   ("qualified", matcher.match_one, "matched", int(p.get("match_batch", 300))),
                                   ("matched", writer.draft_one, "drafted", int(p.get("draft_batch", 30)))):
            for lead in db.leads_by_status(status, limit=n):
                if left() < reserve:
                    return
                res = fn(lead)
                counts[key] += res != "awaiting_portfolio"

    out: dict = {}
    downstream()                      # finish what's already in the pipeline
    paused = _discovery_gate(db, t)

    # Discovery runs in its own thread (own fetcher + search client) while this thread enriches.
    found: dict = {}
    worker = None
    if paused:
        out["discovery"] = paused
    elif left() > reserve + 40:
        deadline = time.monotonic() + left() - reserve

        def discover() -> None:
            from app.graph import Agents

            da = Agents(cfg, db)
            try:
                found["new"], found["sources"] = _discover(cfg, db, da, deadline, d)
            except Exception as e:  # discovery problems must not stop processing
                log.exception("discovery failed: %s", e)
                found["error"] = type(e).__name__
            finally:
                da.close()

        worker = threading.Thread(target=discover, name="discovery", daemon=True)
        worker.start()
    else:
        out["discovery"] = "no time left this tick"

    counts["enriched"] += _enrich(cfg, db, left, reserve, p)
    if worker:
        worker.join(timeout=max(0.0, left() - 5))
        out["discovery"] = "still running at the deadline" if worker.is_alive() else found.get("error", "ran")
        counts["new_leads"] = found.get("new", 0)
        if found.get("sources"):
            out["sources"] = found["sources"]
        counts["enriched"] += _enrich(cfg, db, left, reserve, p)  # today's finds, if time is left
    downstream()                      # carry new leads through to drafts
    out["pipeline"] = counts
    return out
