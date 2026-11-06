#!/usr/bin/env python3
"""Lead generation + outreach CLI.

  python main.py run                 # research -> enrich -> qualify -> match -> draft
  python main.py review              # approve / edit / reject drafts (human approval mode)
  python main.py send                # send approved drafts (limits, delays, suppression)
  python main.py sync-inbox          # mark replies/bounces and suppress them
  python main.py report              # Company | Website | Location | Score | Project | Draft | Status
  python main.py team                # company identity + members and their mailboxes
  python main.py web                 # review page on http://127.0.0.1:8000 (dev)
  python main.py web-user add amartya --admin    # create a review-page login

Add `--as <member>` (see config/team.yaml) to run/draft/send as a specific team member.
"""
from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

from app.config import Config, setup_logging
from app.db import DB

console = Console()


def _ctx(args) -> tuple[Config, DB]:
    cfg = Config()
    setup_logging(cfg, verbose=getattr(args, "verbose", False))
    return cfg, DB(cfg.path(cfg.settings["storage"]["db_path"]))


def _print_results(results: dict) -> None:
    t = Table(title="Run summary")
    t.add_column("Agent")
    t.add_column("Result")
    for k, v in results.items():
        t.add_row(k, ", ".join(f"{a}={b}" for a, b in v.items()) if isinstance(v, dict) else str(v))
    console.print(t)


def _warn_unconfirmed(cfg: Config) -> None:
    pending = [p["domain"] for p in cfg.portfolio if not p.get("confirmed")]
    if pending:
        console.print(f"[yellow]Portfolio not confirmed yet: {', '.join(pending)}. Drafts referencing these "
                      "won't be sent. Edit config/portfolio.yaml and set confirmed: true.[/]")


# ------------------------------------------------------------------ commands
def cmd_run(args) -> None:
    from app.graph import run_pipeline

    cfg, db = _ctx(args)
    _warn_unconfirmed(cfg)
    stages = args.stages.split(",") if args.stages else None
    results = run_pipeline(cfg, db, stages=stages, max_leads=args.max_leads, send=args.send,
                           ignore_window=args.ignore_window, as_member=args.as_member)
    _print_results(results)
    from app.tools.report import push_google_sheets, write_csv

    console.print(f"Report: {write_csv(cfg, db)}")
    push_google_sheets(cfg, db)
    if cfg.settings["sending"]["human_approval"] and db.status_counts().get("drafted"):
        console.print("[bold]Next:[/] python main.py review   then   python main.py send")


def cmd_stage(stage: str):
    def run(args) -> None:
        from app.graph import run_pipeline

        cfg, db = _ctx(args)
        _print_results(run_pipeline(cfg, db, stages=[stage], max_leads=getattr(args, "max_leads", None),
                                    send=(stage == "send"), ignore_window=getattr(args, "ignore_window", False),
                                    as_member=getattr(args, "as_member", None)))
    return run


def cmd_add_lead(args) -> None:
    from app.graph import Agents

    cfg, db = _ctx(args)
    a = Agents(cfg, db)
    lead_id = a.research().add_manual(args.url, args.company, args.city)
    console.print(f"Added lead #{lead_id}" if lead_id else "Already in the database (or skipped).")


def cmd_review(args) -> None:
    from app import review

    cfg, db = _ctx(args)
    _warn_unconfirmed(cfg)
    if args.export:
        console.print(f"Exported drafts to {review.export_csv(cfg, db, args.export)}. Fill the 'decision' column "
                      "with approve/reject, edit subject/body if needed, then: python main.py review --import FILE")
    elif args.import_file:
        console.print(review.import_csv(cfg, db, args.import_file))
    else:
        console.print(review.interactive(cfg, db))


def cmd_send(args) -> None:
    from app.agents.sender import SenderAgent

    cfg, db = _ctx(args)
    _warn_unconfirmed(cfg)
    if args.as_member and not cfg.member(args.as_member):
        sys.exit(f"unknown team member '{args.as_member}'")
    s = SenderAgent(cfg, db, only_member=args.as_member)
    if args.dry_run:
        s.dry_run = True
    console.print(s.run(ignore_window=args.ignore_window, limit=args.limit))


def cmd_sync_inbox(args) -> None:
    from app.tools.imap_sync import sync_inbox

    cfg, db = _ctx(args)
    console.print(sync_inbox(cfg, db, days=args.days))


def cmd_suppress(args) -> None:
    cfg, db = _ctx(args)
    if args.list:
        t = Table("value", "kind", "reason", "added")
        for r in db.suppression_list():
            t.add_row(r["value"], r["kind"], r["reason"], r["created_at"])
        console.print(t)
        return
    for v in args.values:
        kind = "email" if "@" in v else "domain"
        db.suppress(v, kind, args.reason)
        console.print(f"Suppressed {kind} {v} ({args.reason})")


def cmd_report(args) -> None:
    from app.tools.report import push_google_sheets, rows, write_csv

    cfg, db = _ctx(args)
    t = Table("Company", "Website", "Location", "Score", "Matched Project", "Email Draft", "Status",
              show_lines=True, title="Leads")
    for r in rows(cfg, db, args.status)[-args.limit:]:
        draft = r[5] if args.full else r[5].split("\n")[0]  # subject line; --full for the whole draft
        t.add_row(str(r[0])[:30], r[1], r[2], str(r[3]), r[4], draft, r[6])
    console.print(t)
    console.print(db.status_counts())
    console.print(f"CSV: {write_csv(cfg, db, args.csv)}")
    if args.sheets:
        push_google_sheets(cfg, db)


def cmd_show(args) -> None:
    cfg, db = _ctx(args)
    lead = db.get_lead(args.id)
    if not lead:
        console.print("not found")
        return
    for k in lead.keys():
        if k != "signals":
            console.print(f"[bold]{k}[/]: {lead[k]}")
    console.print("[bold]events[/]:")
    for e in db.events_for(args.id):
        console.print(f"  {e['ts']} {e['agent']}:{e['event']} {e['detail'] or ''}")


def cmd_test_smtp(args) -> None:
    from app.agents.sender import SenderAgent

    cfg, db = _ctx(args)
    console.print(SenderAgent(cfg, db).test_connection(args.as_member))


def cmd_team(args) -> None:
    cfg, db = _ctx(args)
    c = cfg.company
    console.print(f"[bold]{c['legal_name']}[/] ({c['short_name']}) · {c['website']} · {c['email']}")
    console.print(f"assignment: {cfg.team.get('assignment', 'default')} · default member: {cfg.team['default_member']}")
    open_leads = db.assigned_counts()
    t = Table("id", "name", "title", "From (as recipients see it)", "mailbox", "credentials", "open leads", "sent 24h")
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    for m in cfg.members:
        mb = cfg.mailbox(m)
        creds = "[green]set[/]" if mb["smtp_user"] and mb["smtp_password"] else "[red]missing[/]"
        t.add_row(m["id"] + ("" if m.get("active", True) else " (inactive)"), m["name"], m["title"],
                  f"{cfg.from_name(m)} <{mb['from_email']}>", mb["id"], creds,
                  str(open_leads.get(m["id"], 0)), str(db.sent_since(since, mb["id"])))
    console.print(t)


def cmd_assign(args) -> None:
    """Hand leads to another team member: re-signs the draft, or re-matches if the
    current project is someone else's personal portfolio."""
    from app.agents.email_writer import apply_signature, check_draft
    from app.graph import Agents

    cfg, db = _ctx(args)
    member = cfg.member(args.member)
    if not member or args.member != member["id"]:
        sys.exit(f"unknown team member '{args.member}'")
    a = Agents(cfg, db)
    for lead_id in args.ids:
        lead = db.get_lead(lead_id)
        if not lead:
            console.print(f"#{lead_id}: not found")
            continue
        if lead["status"] in ("sent", "replied", "bounced"):
            console.print(f"#{lead_id}: already {lead['status']}; not reassigned")
            continue
        proj = cfg.project(lead["matched_project"] or "")
        if proj and not cfg.project_allowed_for(proj, member["id"]):
            db.update_lead(lead_id, status="qualified")
            a.matching().match_one(db.get_lead(lead_id), member_id=member["id"])
            if db.get_lead(lead_id)["status"] == "matched":
                a.email().draft_one(db.get_lead(lead_id))
            console.print(f"#{lead_id}: re-matched and re-drafted for {member['name']}")
        elif lead["email_body"]:
            body = apply_signature(cfg, lead["email_body"], member)
            problems = check_draft(cfg, {**dict(lead), "sender_id": member["id"]}, lead["email_subject"], body)
            status = "drafted" if lead["status"] == "approved" else lead["status"]  # re-review after a change
            db.update_lead(lead_id, sender_id=member["id"], email_body=body, validation=problems, status=status)
            console.print(f"#{lead_id}: now signed by {member['name']}" + (" (needs re-approval)" if status != lead["status"] else ""))
        else:
            db.update_lead(lead_id, sender_id=member["id"])
            console.print(f"#{lead_id}: assigned to {member['name']}")
        db.log("assign", "assigned", lead_id, member["id"])


def cmd_test_llm(args) -> None:
    from app.llm import LLM

    cfg, _ = _ctx(args)
    llm = LLM(cfg)
    console.print(f"provider={llm.provider} model={llm.model} enabled={llm.enabled}")
    console.print(llm.json("You are a test.", 'Return {"ok": true}'))


def cmd_web(args) -> None:
    from app.web import create_app

    cfg, db = _ctx(args)
    console.print(f"Review page (dev mode): http://{args.host}:{args.port}  · Ctrl+C to stop")
    create_app(dev=True, db=db).run(host=args.host, port=args.port, debug=False)


def cmd_web_user(args) -> None:
    import getpass

    from werkzeug.security import generate_password_hash

    cfg, db = _ctx(args)
    if args.action == "list":
        t = Table("username", "name", "role", "last login")
        for u in db.list_users():
            m = cfg.member(u["username"])
            t.add_row(u["username"], m["name"] if m and m["id"] == u["username"] else "[red]not in team.yaml[/]",
                      "admin" if u["is_admin"] else "member", u["last_login_at"] or "never")
        console.print(t)
        return
    if not args.username:
        sys.exit("username required (a member id from config/team.yaml)")
    username = args.username.lower()
    if args.action == "remove":
        console.print("Removed." if db.delete_user(username) else "No such user.")
        return
    member = cfg.member(username)
    if not member or member["id"] != username:
        sys.exit(f"'{username}' is not a member id in config/team.yaml. Add them there first.")
    existing = db.get_user(username)
    if args.action == "passwd" and not existing:
        sys.exit("No such user; use `web-user add`.")
    pw = args.password or getpass.getpass(f"New password for {member['name']}: ")
    if not args.password and pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords don't match.")
    if len(pw) < 10:
        sys.exit("Use at least 10 characters.")
    is_admin = args.admin if args.action == "add" else bool(existing["is_admin"])
    db.upsert_user(username, generate_password_hash(pw), is_admin)
    console.print(f"{'Updated' if existing else 'Created'} web login '{username}' ({'admin' if is_admin else 'member'}).")


def cmd_backup(args) -> None:
    cfg, db = _ctx(args)
    st = cfg.settings["storage"]
    dest = db.backup(cfg.path(st.get("backup_dir", "data/backups")), int(st.get("backup_keep_days", 14)))
    console.print(f"Backup written: {dest}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Free multi-agent lead generation + outreach for Indian SMEs")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="full pipeline (drafts only unless --send)")
    r.add_argument("--max-leads", type=int, default=None, help="max new leads to discover this run")
    r.add_argument("--stages", help="comma list: research,enrich,qualify,match,write,send")
    r.add_argument("--send", action="store_true", help="also run the Sender Agent at the end")
    r.add_argument("--ignore-window", action="store_true")
    r.add_argument("--as", dest="as_member", metavar="MEMBER", help="team member (config/team.yaml) who signs new drafts")
    r.set_defaults(func=cmd_run)

    for name, stage, help_ in [("research", "research", "Research Agent only"), ("enrich", "enrich", "Enrichment Agent"),
                               ("qualify", "qualify", "Qualification Agent"), ("match", "match", "Matching Agent"),
                               ("draft", "write", "Email Agent")]:
        s = sub.add_parser(name, help=help_)
        s.add_argument("--max-leads", type=int, default=None)
        if stage in ("match", "write"):
            s.add_argument("--as", dest="as_member", metavar="MEMBER", help="team member who signs these drafts")
        s.set_defaults(func=cmd_stage(stage))

    a = sub.add_parser("add-lead", help="seed a company website manually")
    a.add_argument("url")
    a.add_argument("--company")
    a.add_argument("--city")
    a.set_defaults(func=cmd_add_lead)

    rv = sub.add_parser("review", help="human approval of drafts")
    rv.add_argument("--export", metavar="CSV", help="export drafts to CSV for spreadsheet review")
    rv.add_argument("--import", dest="import_file", metavar="CSV", help="import decisions from CSV")
    rv.set_defaults(func=cmd_review)

    sd = sub.add_parser("send", help="Sender Agent: send approved emails")
    sd.add_argument("--dry-run", action="store_true")
    sd.add_argument("--ignore-window", action="store_true", help="send outside business hours")
    sd.add_argument("--limit", type=int, default=None, help="send at most N now (still capped by daily limit)")
    sd.add_argument("--as", dest="as_member", metavar="MEMBER", help="only send leads assigned to this member")
    sd.set_defaults(func=cmd_send)

    si = sub.add_parser("sync-inbox", help="read replies/bounces via IMAP and suppress them")
    si.add_argument("--days", type=int, default=30)
    si.set_defaults(func=cmd_sync_inbox)

    su = sub.add_parser("suppress", help="add emails/domains to the suppression list")
    su.add_argument("values", nargs="*")
    su.add_argument("--reason", default="manual")
    su.add_argument("--list", action="store_true")
    su.set_defaults(func=cmd_suppress)

    rp = sub.add_parser("report", help="show/export leads table")
    rp.add_argument("--status")
    rp.add_argument("--limit", type=int, default=50)
    rp.add_argument("--full", action="store_true", help="show full email drafts")
    rp.add_argument("--csv", default=None)
    rp.add_argument("--sheets", action="store_true", help="also push to Google Sheets")
    rp.set_defaults(func=cmd_report)

    sh = sub.add_parser("show", help="all fields + event log for one lead")
    sh.add_argument("id", type=int)
    sh.set_defaults(func=cmd_show)

    ts = sub.add_parser("test-smtp", help="check SMTP login (shared mailbox, or a member's with --as)")
    ts.add_argument("--as", dest="as_member", metavar="MEMBER")
    ts.set_defaults(func=cmd_test_smtp)

    sub.add_parser("team", help="show company identity, members and mailboxes").set_defaults(func=cmd_team)

    w = sub.add_parser("web", help="run the review page locally (dev mode, http)")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(func=cmd_web)

    wu = sub.add_parser("web-user", help="manage review-page logins (username = team member id)")
    wu.add_argument("action", choices=["add", "passwd", "remove", "list"])
    wu.add_argument("username", nargs="?")
    wu.add_argument("--admin", action="store_true", help="can act on everyone's leads")
    wu.add_argument("--password", help=argparse.SUPPRESS)  # for scripts/tests; prefer the prompt
    wu.set_defaults(func=cmd_web_user)

    sub.add_parser("backup", help="copy the database to storage.backup_dir").set_defaults(func=cmd_backup)

    asg = sub.add_parser("assign", help="hand leads to another team member")
    asg.add_argument("member")
    asg.add_argument("ids", nargs="+", type=int)
    asg.set_defaults(func=cmd_assign)
    sub.add_parser("test-llm", help="check LLM key").set_defaults(func=cmd_test_llm)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
