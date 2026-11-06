"""SQLite storage: leads, suppression list, per-lead event log, query history, HTTP cache."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    company         TEXT NOT NULL,
    website         TEXT,
    domain          TEXT UNIQUE,
    source          TEXT,
    source_url      TEXT,
    query           TEXT,
    snippet         TEXT,
    city_hint       TEXT,
    location        TEXT,
    city            TEXT,
    state           TEXT,
    tier            TEXT,
    email           TEXT,
    all_emails      TEXT,
    phone           TEXT,
    whatsapp        INTEGER DEFAULT 0,
    linkedin        TEXT,
    industry        TEXT,
    signals         TEXT,          -- JSON from Enrichment Agent
    rule_score      REAL,
    llm_score       REAL,
    score           INTEGER,
    needs           TEXT,          -- JSON list
    reasons         TEXT,          -- JSON list
    observed_problem TEXT,
    matched_project TEXT,
    match_reason    TEXT,
    sender_id       TEXT,          -- team.yaml member who signs/sends this email
    sent_from       TEXT,          -- mailbox id used ('shared' or 'member:<id>')
    email_subject   TEXT,
    email_body      TEXT,
    validation      TEXT,          -- JSON list of rule violations (empty = clean)
    status          TEXT NOT NULL DEFAULT 'discovered',
    error           TEXT,
    message_id      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    approved_at     TEXT,
    sent_at         TEXT
);

CREATE TABLE IF NOT EXISTS suppression (
    value       TEXT PRIMARY KEY,  -- lower-cased email or domain
    kind        TEXT NOT NULL,     -- email | domain
    reason      TEXT NOT NULL,     -- emailed | bounced | replied | unsubscribed | manual
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id  INTEGER,
    ts       TEXT NOT NULL,
    agent    TEXT NOT NULL,
    event    TEXT NOT NULL,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON events(lead_id);

CREATE TABLE IF NOT EXISTS queries (
    query    TEXT PRIMARY KEY,
    source   TEXT,
    ran_at   TEXT,
    results  INTEGER
);

CREATE TABLE IF NOT EXISTS users (
    username       TEXT PRIMARY KEY,   -- team.yaml member id
    password_hash  TEXT NOT NULL,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_login_at  TEXT
);

CREATE TABLE IF NOT EXISTS login_failures (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username  TEXT NOT NULL,
    ip        TEXT,
    ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS http_cache (
    url          TEXT PRIMARY KEY,
    fetched_at   TEXT NOT NULL,
    status       INTEGER,
    final_url    TEXT,
    content_type TEXT,
    rendered     INTEGER DEFAULT 0,
    body         TEXT
);
"""

# Indexes are created after migrations so older databases get the new columns first.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_sent ON leads(sent_from, sent_at);
"""

# Columns added after the first release: ALTER TABLE on older databases.
MIGRATIONS = {"leads": {"sender_id": "TEXT", "sent_from": "TEXT"}}

# Lead status lifecycle
STATUSES = [
    "discovered",      # Research Agent found it
    "no_website",      # directory listing without a site (kept for manual follow-up)
    "enriched",        # Enrichment Agent extracted details
    "unreachable",     # site down / non-HTML
    "blocked_robots",  # robots.txt disallows us
    "discarded",       # Qualification score below threshold / not ICP
    "qualified",
    "matched",
    "needs_contact",   # qualified + drafted, but no public email address
    "drafted",         # email written, waiting for human review
    "approved",
    "rejected",
    "sent",
    "failed",
    "suppressed",
    "bounced",
    "replied",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(INDEXES)
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        for table, cols in MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    # ---------------------------------------------------------------- leads
    def find_lead(self, domain: str | None = None, company: str | None = None, city: str | None = None):
        if domain:
            return self.conn.execute("SELECT * FROM leads WHERE domain=?", (domain,)).fetchone()
        if company:
            return self.conn.execute(
                "SELECT * FROM leads WHERE lower(company)=lower(?) AND coalesce(lower(city_hint),'')=coalesce(lower(?),'')",
                (company, city),
            ).fetchone()
        return None

    def add_lead(self, **fields: Any) -> int | None:
        """Insert unless the domain (or company+city when no domain) already exists."""
        if self.find_lead(domain=fields.get("domain")) if fields.get("domain") else self.find_lead(
            company=fields.get("company"), city=fields.get("city_hint")
        ):
            return None
        fields.setdefault("status", "discovered")
        fields["created_at"] = fields["updated_at"] = now()
        cols = ", ".join(fields)
        qs = ", ".join("?" for _ in fields)
        with self._lock:
            try:
                cur = self.conn.execute(f"INSERT INTO leads ({cols}) VALUES ({qs})", list(fields.values()))
            except sqlite3.IntegrityError:
                return None
        return cur.lastrowid

    def update_lead(self, lead_id: int, **fields: Any) -> None:
        for k, v in list(fields.items()):
            if isinstance(v, (list, dict)):
                fields[k] = json.dumps(v, ensure_ascii=False)
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE leads SET {sets} WHERE id=?", [*fields.values(), lead_id])

    def get_lead(self, lead_id: int):
        return self.conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()

    def leads_by_status(self, *statuses: str, limit: int | None = None) -> list[sqlite3.Row]:
        q = f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY score DESC, id"
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.conn.execute(q, statuses).fetchall()

    def all_leads(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.conn.execute("SELECT * FROM leads WHERE status=? ORDER BY id", (status,)).fetchall()
        return self.conn.execute("SELECT * FROM leads ORDER BY id").fetchall()

    def status_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT status, count(*) FROM leads GROUP BY status")}

    def sent_since(self, since_iso: str, mailbox: str | None = None) -> int:
        """Emails actually sent since a time, optionally from one mailbox (daily limits are per mailbox)."""
        if mailbox:
            return self.conn.execute("SELECT count(*) FROM leads WHERE sent_from=? AND sent_at>=?",
                                     (mailbox, since_iso)).fetchone()[0]
        return self.conn.execute("SELECT count(*) FROM leads WHERE sent_at>=?", (since_iso,)).fetchone()[0]

    def assigned_counts(self) -> dict[str, int]:
        """Open leads per team member (used for round-robin assignment)."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT sender_id, count(*) FROM leads WHERE sender_id IS NOT NULL "
            "AND status IN ('matched','drafted','needs_contact','approved') GROUP BY sender_id")}

    # ----------------------------------------------------------- suppression
    def suppress(self, value: str, kind: str, reason: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO suppression(value, kind, reason, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(value) DO UPDATE SET reason=excluded.reason",
                (value.lower().strip(), kind, reason, now()),
            )

    def is_suppressed(self, email: str | None, domain: str | None) -> str | None:
        """Returns the suppression reason, or None if OK to contact."""
        keys = [k.lower() for k in (email, domain) if k]
        if email and "@" in email:
            keys.append(email.split("@", 1)[1].lower())
        for k in keys:
            row = self.conn.execute("SELECT reason FROM suppression WHERE value=?", (k,)).fetchone()
            if row:
                return row[0]
        return None

    def suppression_list(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM suppression ORDER BY created_at DESC").fetchall()

    # ----------------------------------------------------------------- users
    def get_user(self, username: str):
        return self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

    def list_users(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users ORDER BY username").fetchall()

    def upsert_user(self, username: str, password_hash: str, is_admin: bool) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, is_admin=excluded.is_admin",
                (username, password_hash, int(is_admin), now()))

    def delete_user(self, username: str) -> bool:
        with self._lock:
            return self.conn.execute("DELETE FROM users WHERE username=?", (username,)).rowcount > 0

    def record_login(self, username: str, ok: bool, ip: str | None) -> None:
        with self._lock:
            if ok:
                self.conn.execute("UPDATE users SET last_login_at=? WHERE username=?", (now(), username))
                self.conn.execute("DELETE FROM login_failures WHERE username=?", (username,))
            else:
                self.conn.execute("INSERT INTO login_failures(username, ip, ts) VALUES (?,?,?)", (username, ip, now()))

    def recent_login_failures(self, username: str, minutes: int) -> int:
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        return self.conn.execute("SELECT count(*) FROM login_failures WHERE username=? AND ts>=?",
                                 (username, since)).fetchone()[0]

    # ---------------------------------------------------------------- backup
    def backup(self, target_dir: str | Path, keep_days: int = 14) -> Path:
        """Consistent online copy of the database (safe while the web app and timers run)."""
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / f"leads-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        out = sqlite3.connect(str(dest))
        with out:
            self.conn.backup(out)
        out.close()
        cutoff = datetime.now().timestamp() - keep_days * 86400
        for old in target_dir.glob("leads-*.db"):
            if old.stat().st_mtime < cutoff:
                old.unlink()
        return dest

    def last_event(self, agent: str, event: str | None = None):
        q = "SELECT * FROM events WHERE agent=?" + (" AND event=?" if event else "") + " ORDER BY id DESC LIMIT 1"
        return self.conn.execute(q, (agent, event) if event else (agent,)).fetchone()

    # ---------------------------------------------------------------- events
    def log(self, agent: str, event: str, lead_id: int | None = None, detail: Any = None) -> None:
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(lead_id, ts, agent, event, detail) VALUES (?,?,?,?,?)",
                (lead_id, now(), agent, event, detail),
            )

    def events_for(self, lead_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM events WHERE lead_id=? ORDER BY id", (lead_id,)).fetchall()

    # --------------------------------------------------------------- queries
    def query_ran(self, query: str) -> bool:
        return self.conn.execute("SELECT 1 FROM queries WHERE query=?", (query,)).fetchone() is not None

    def record_query(self, query: str, source: str, results: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO queries(query, source, ran_at, results) VALUES (?,?,?,?)",
                (query, source, now(), results),
            )

    # ------------------------------------------------------------ http cache
    def cache_get(self, url: str, max_age_hours: float):
        row = self.conn.execute("SELECT * FROM http_cache WHERE url=?", (url,)).fetchone()
        if not row:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
        return row if age < timedelta(hours=max_age_hours) else None

    def cache_put(self, url: str, status: int, final_url: str, content_type: str, body: str, rendered: bool) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO http_cache(url, fetched_at, status, final_url, content_type, rendered, body) "
                "VALUES (?,?,?,?,?,?,?)",
                (url, now(), status, final_url, content_type, int(rendered), body),
            )


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
