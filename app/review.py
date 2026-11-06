"""Human approval mode: review drafts in the terminal, or via CSV round-trip."""
from __future__ import annotations

import csv
import os
import subprocess
import tempfile

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from app.config import Config
from app.db import DB, loads, now

console = Console()


def _show(cfg: Config, lead, idx: int, total: int) -> None:
    proj = cfg.project(lead["matched_project"]) or {}
    problems = loads(lead["validation"], [])
    head = (f"[bold]{lead['company']}[/]  ({idx}/{total})\n"
            f"{lead['website']}  ·  {lead['location'] or '?'}  ·  score [bold]{lead['score']}[/]/10\n"
            f"To: {lead['email']}   From: {cfg.from_name(cfg.member(lead['sender_id']) or cfg.member(None))}   "
            f"Project: {proj.get('domain')} "
            f"{'[green]confirmed[/]' if proj.get('confirmed') else '[red]UNCONFIRMED (will not send)[/]'}\n"
            f"Why: {'; '.join(loads(lead['reasons'], [])[:6])}")
    console.print(Panel(head, title="Lead", border_style="cyan"))
    console.print(Panel(f"[bold]Subject:[/] {lead['email_subject']}\n\n{lead['email_body']}", title="Draft",
                        border_style="red" if problems else "green"))
    if problems:
        console.print("[red]Rule problems:[/] " + "; ".join(problems))


def _edit(subject: str, body: str) -> tuple[str, str]:
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "nano")
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(f"Subject: {subject}\n\n{body}")
        path = tf.name
    try:
        subprocess.call([editor, path])
        text = open(path, encoding="utf-8").read()
    finally:
        os.unlink(path)
    first, _, rest = text.partition("\n")
    return first.removeprefix("Subject:").strip(), rest.strip("\n")


def interactive(cfg: Config, db: DB) -> dict:
    from app.agents.email_writer import check_draft

    leads = db.leads_by_status("drafted")
    counts = {"approved": 0, "rejected": 0, "skipped": 0}
    if not leads:
        console.print("No drafts waiting for review.")
        return counts
    for i, lead in enumerate(leads, 1):
        while True:
            lead = db.get_lead(lead["id"])
            _show(cfg, lead, i, len(leads))
            choice = Prompt.ask("[a]pprove  [e]dit  [r]eject  [s]kip  [q]uit", choices=list("aersq"), default="s")
            if choice == "e":
                subj, body = _edit(lead["email_subject"], lead["email_body"])
                problems = check_draft(cfg, dict(lead), subj, body)
                db.update_lead(lead["id"], email_subject=subj, email_body=body, validation=problems)
                db.log("review", "edited", lead["id"], {"problems": problems})
                continue
            if choice == "a":
                if loads(lead["validation"], []):
                    console.print("[red]Fix the rule problems first (edit), or reject.[/]")
                    continue
                db.update_lead(lead["id"], status="approved", approved_at=now())
                db.log("review", "approved", lead["id"])
                counts["approved"] += 1
            elif choice == "r":
                db.update_lead(lead["id"], status="rejected")
                db.log("review", "rejected", lead["id"])
                counts["rejected"] += 1
            elif choice == "s":
                counts["skipped"] += 1
            elif choice == "q":
                return counts
            break
    return counts


def export_csv(cfg: Config, db: DB, path: str) -> str:
    target = cfg.path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "decision", "company", "website", "email", "score", "sender", "project", "subject", "body",
                    "problems"])
        for r in db.leads_by_status("drafted"):
            w.writerow([r["id"], "", r["company"], r["website"], r["email"], r["score"], r["sender_id"], r["matched_project"],
                        r["email_subject"], r["email_body"], "; ".join(loads(r["validation"], []))])
    return str(target)


def import_csv(cfg: Config, db: DB, path: str) -> dict:
    """decision column: approve | reject | (blank = leave). Edited subject/body are re-validated."""
    from app.agents.email_writer import check_draft

    counts = {"approved": 0, "rejected": 0, "invalid": 0}
    with open(cfg.path(path), newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lead = db.get_lead(int(row["id"]))
            if not lead or lead["status"] != "drafted":
                continue
            decision = (row.get("decision") or "").strip().lower()
            subj, body = row.get("subject") or lead["email_subject"], row.get("body") or lead["email_body"]
            problems = check_draft(cfg, dict(lead), subj, body)
            db.update_lead(lead["id"], email_subject=subj, email_body=body, validation=problems)
            if decision.startswith("a"):
                if problems:
                    counts["invalid"] += 1
                    console.print(f"[red]#{lead['id']} {lead['company']}: not approved, {'; '.join(problems)}[/]")
                    continue
                db.update_lead(lead["id"], status="approved", approved_at=now())
                db.log("review", "approved", lead["id"], "csv")
                counts["approved"] += 1
            elif decision.startswith("r"):
                db.update_lead(lead["id"], status="rejected")
                db.log("review", "rejected", lead["id"], "csv")
                counts["rejected"] += 1
    return counts
