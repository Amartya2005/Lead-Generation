"""Provider back-off kept in the database (kv), so it survives across serverless ticks.

After a rate limit or failure the provider is paused for `first` minutes, doubling on each
further failure up to `cap`; a success resets it. Callers skip the provider while paused
instead of sleeping inside a time-bounded tick.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from app.db import DB

log = logging.getLogger("backoff")


def cooling_down(db: DB, key: str) -> str | None:
    state = json.loads(db.kv_get(key, "{}") or "{}")
    until = state.get("until")
    if until and datetime.fromisoformat(until) > datetime.now(timezone.utc):
        return f"backing off until {until} ({state.get('why', '')})"
    return None


def back_off(db: DB, key: str, why: str, first: int = 15, cap: int = 240) -> int:
    state = json.loads(db.kv_get(key, "{}") or "{}")
    minutes = min(int(state["minutes"]) * 2, cap) if state.get("minutes") else first
    until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    db.kv_set(key, json.dumps({"until": until, "minutes": minutes, "why": why[:200]}))
    log.warning("%s: %s; backing off %d min", key, why, minutes)
    return minutes


def reset(db: DB, key: str) -> None:
    if db.kv_get(key) not in (None, "", "{}"):
        db.kv_set(key, "{}")
