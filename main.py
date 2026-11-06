#!/usr/bin/env python3
"""Lead generation + outreach CLI.

  python main.py run                 # research -> enrich -> qualify -> match -> draft
  python main.py review              # approve / edit / reject drafts (human approval mode)
  python main.py send                # send approved drafts (limits, delays, suppression)
  python main.py sync-inbox          # mark replies/bounces and suppress them
  python main.py report              # Company | Website | Location | Score | Project | Draft | Status
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
                           ignore_window=args.ignore_window)
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
                                    send=(stage == "send"), ignore_window=getattr(args, "ignore_window", False)))
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
    s = SenderAgent(cfg, db)
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
    console.print(SenderAgent(cfg, db).test_connection())


def cmd_test_llm(args) -> None:
    from app.llm import LLM

    cfg, _ = _ctx(args)
    llm = LLM(cfg)
    console.print(f"provider={llm.provider} model={llm.model} enabled={llm.enabled}")
    console.print(llm.json("You are a test.", 'Return {"ok": true}'))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Free multi-agent lead generation + outreach for Indian SMEs")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="full pipeline (drafts only unless --send)")
    r.add_argument("--max-leads", type=int, default=None, help="max new leads to discover this run")
    r.add_argument("--stages", help="comma list: research,enrich,qualify,match,write,send")
    r.add_argument("--send", action="store_true", help="also run the Sender Agent at the end")
    r.add_argument("--ignore-window", action="store_true")
    r.set_defaults(func=cmd_run)

    for name, stage, help_ in [("research", "research", "Research Agent only"), ("enrich", "enrich", "Enrichment Agent"),
                               ("qualify", "qualify", "Qualification Agent"), ("match", "match", "Matching Agent"),
                               ("draft", "write", "Email Agent")]:
        s = sub.add_parser(name, help=help_)
        s.add_argument("--max-leads", type=int, default=None)
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

    sub.add_parser("test-smtp", help="check SMTP login").set_defaults(func=cmd_test_smtp)
    sub.add_parser("test-llm", help="check LLM key").set_defaults(func=cmd_test_llm)

    args = p.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
