"""Per-source lead-discovery analytics (daily / weekly throughput).

Discovery outcomes are counted when they happen, in the existing `kv` table:
    stats:<IST date>:<source>:<metric>    metric = candidates | added | duplicate | unresolved | rejected:<reason>
The later funnel (enriched, site rejected, qualified, ready for review) comes from the existing
`events` table, joined to each lead's source. No schema change.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import Config
from app.db import DB

FUNNEL = ("enriched", "site_rejected", "qualified", "ready_for_review")


def _tz(cfg: Config) -> ZoneInfo:
    return ZoneInfo(cfg.settings["sending"].get("send_window", {}).get("timezone", "Asia/Kolkata"))


def today(cfg: Config) -> str:
    return datetime.now(_tz(cfg)).strftime("%Y-%m-%d")


def source_group(source: str | None) -> str:
    """'directory:justdial.com' -> 'directory'; 'osm', 'news:inc42', 'duckduckgo' unchanged."""
    s = source or "unknown"
    return "directory" if s.startswith("directory:") else s


def record(cfg: Config, db: DB, source: str | None, outcome: str) -> None:
    prefix = f"stats:{today(cfg)}:{source_group(source)}:"
    db.kv_incr(prefix + "candidates")
    db.kv_incr(prefix + ("duplicate" if outcome == "seen_again" else outcome))


def _days(cfg: Config, n: int) -> list[str]:
    now = datetime.now(_tz(cfg))
    return [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def throughput(cfg: Config, db: DB, days: int = 1) -> dict[str, Counter]:
    """{source: Counter(candidates, added, duplicate, rejected:<why>, unresolved, enriched, ...)}"""
    from app.tools.report import READY, review_status

    span = _days(cfg, days)
    out: dict[str, Counter] = defaultdict(Counter)
    for r in db.query("SELECT key, value FROM kv WHERE key >= ? AND key < ?", (f"stats:{span[0]}", f"stats:{span[-1]}~")):
        _, day, source, metric = r["key"].split(":", 3)
        if day in span:
            out[source][metric] += int(r["value"] or 0)

    start = datetime.strptime(span[0], "%Y-%m-%d").replace(tzinfo=_tz(cfg)).astimezone(timezone.utc)
    rows = db.query(
        "SELECT e.lead_id, e.agent, e.event, l.source FROM events e JOIN leads l ON l.id = e.lead_id "
        "WHERE e.ts >= ? AND ((e.agent = 'enrichment' AND e.event IN ('enriched', 'not_company_site')) "
        "OR (e.agent = 'qualification' AND e.event = 'qualified') OR (e.agent = 'email' AND e.event = 'drafted'))",
        (start.isoformat(timespec="seconds"),))
    seen: set[tuple] = set()
    drafted: dict[int, str] = {}
    for r in rows:
        src = source_group(r["source"])
        metric = {"enriched": "enriched", "not_company_site": "site_rejected", "qualified": "qualified"}.get(r["event"])
        if metric and (r["lead_id"], metric) not in seen:
            seen.add((r["lead_id"], metric))
            out[src][metric] += 1
        elif r["event"] == "drafted":
            drafted[r["lead_id"]] = src
    for lead_id, src in drafted.items():
        lead = db.get_lead(lead_id)
        if lead and review_status(cfg, lead) == READY:
            out[src]["ready_for_review"] += 1
    return dict(out)


def daily_added(cfg: Config, db: DB, days: int = 7) -> list[tuple[str, int]]:
    span = _days(cfg, days)
    totals = Counter()
    for r in db.query("SELECT key, value FROM kv WHERE key >= ? AND key < ?", (f"stats:{span[0]}", f"stats:{span[-1]}~")):
        _, day, _, metric = r["key"].split(":", 3)
        if metric == "added":
            totals[day] += int(r["value"] or 0)
    return [(d, totals[d]) for d in span]


def prune(cfg: Config, db: DB, keep_days: int = 90) -> int:
    cutoff = _days(cfg, keep_days)[0]
    return db.execute("DELETE FROM kv WHERE key >= 'stats:' AND key < ?", (f"stats:{cutoff}",)).rowcount
