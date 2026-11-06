"""Per-lead output: Company | Website | Location | Score | Matched Project | Email Draft | Status.
Writes CSV, optionally mirrors to Google Sheets (free service account)."""
from __future__ import annotations

import csv
import logging

from app.config import Config
from app.db import DB

log = logging.getLogger("report")
COLUMNS = ["Company", "Website", "Location", "Score", "Matched Project", "Email Draft", "Status",
           "Email", "Needs", "Reasons", "Updated"]


def rows(cfg: Config, db: DB, status: str | None = None) -> list[list]:
    from app.db import loads

    out = []
    for r in db.all_leads(status):
        proj = cfg.project(r["matched_project"]) if r["matched_project"] else None
        draft = f"Subject: {r['email_subject']}\n\n{r['email_body']}" if r["email_body"] else ""
        out.append([
            r["company"], r["website"] or "", r["location"] or r["city_hint"] or "", r["score"] if r["score"] is not None else "",
            proj["domain"] if proj else "", draft, r["status"], r["email"] or "",
            ", ".join(loads(r["needs"], [])), "; ".join(loads(r["reasons"], [])), r["updated_at"],
        ])
    return out


def write_csv(cfg: Config, db: DB, path: str | None = None, status: str | None = None) -> str:
    target = cfg.path(path or cfg.settings["storage"]["csv_report"])
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        w.writerows(rows(cfg, db, status))
    return str(target)


def push_google_sheets(cfg: Config, db: DB) -> bool:
    gs = cfg.settings["storage"].get("google_sheets", {})
    if not gs.get("enabled"):
        return False
    try:
        import gspread
    except ImportError:
        log.warning("Google Sheets enabled but gspread not installed: pip install gspread google-auth")
        return False
    try:
        client = gspread.service_account(filename=str(cfg.path(gs["service_account_file"])))
        sh = client.open(gs["spreadsheet"])
        try:
            ws = sh.worksheet(gs["worksheet"])
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(gs["worksheet"], rows=1000, cols=len(COLUMNS))
        ws.clear()
        ws.update([COLUMNS] + [[str(c) for c in row] for row in rows(cfg, db)], "A1")
        log.info("Pushed %d leads to Google Sheets '%s'", len(rows(cfg, db)), gs["spreadsheet"])
        return True
    except Exception as e:
        log.warning("Google Sheets sync failed: %s", e)
        return False
