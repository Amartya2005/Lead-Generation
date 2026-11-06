"""Sender Agent: sends approved emails over Gmail/Outlook SMTP with safety controls.

Checks run immediately before each send (in this order):
  1. human approval: status must be 'approved' (or 'drafted' when approval is off)
  2. draft passes email_rules.validate() again (catches bad manual edits)
  3. matched portfolio project has confirmed: true
  4. address / domain not on the suppression list
  5. at most `max_per_domain` emails per company domain
  6. daily limit (rolling 24h, counted from the event log)
  7. send window (IST business hours, Mon-Sat by default)
Random delay between sends. Every outcome is logged to `events`.
After sending, the address is suppressed so no lead is ever emailed twice.
"""
from __future__ import annotations

import logging
import random
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from zoneinfo import ZoneInfo

from app.config import Config
from app.db import DB
from app.agents.email_writer import check_draft

log = logging.getLogger("sender")


class SendBlocked(Exception):
    pass


class SenderAgent:
    name = "sender"

    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        s = cfg.settings["sending"]
        self.daily_limit = int(s.get("daily_limit", 25))
        self.min_delay, self.max_delay = float(s.get("min_delay_seconds", 90)), float(s.get("max_delay_seconds", 300))
        self.approval = bool(s.get("human_approval", True))
        self.dry_run = bool(s.get("dry_run", False))
        self.max_per_domain = int(s.get("max_per_domain", 1))
        self.window = s.get("send_window", {})
        self._smtp: smtplib.SMTP | None = None

    # ------------------------------------------------------------ guards
    def sent_last_24h(self) -> int:
        return self.db.sent_since((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds"))

    def in_window(self) -> bool:
        if not self.window:
            return True
        now = datetime.now(ZoneInfo(self.window.get("timezone", "Asia/Kolkata")))
        return (now.weekday() in self.window.get("weekdays", range(7))
                and self.window.get("start_hour", 0) <= now.hour < self.window.get("end_hour", 24))

    def check(self, lead) -> None:
        if not lead["email"]:
            raise SendBlocked("no email address")
        if self.approval and lead["status"] != "approved":
            raise SendBlocked("not approved (human approval mode is on)")
        proj = self.cfg.project(lead["matched_project"] or "")
        if not proj:
            raise SendBlocked("no matched portfolio project")
        if not proj.get("confirmed"):
            raise SendBlocked(f"portfolio project '{proj['id']}' is not confirmed in config/portfolio.yaml")
        problems = check_draft(self.cfg, dict(lead), lead["email_subject"], lead["email_body"])
        if problems:
            raise SendBlocked("draft breaks email rules: " + "; ".join(problems))
        reason = self.db.is_suppressed(lead["email"], lead["domain"])
        if reason:
            raise SendBlocked(f"suppressed ({reason})")
        same_domain = self.db.conn.execute(
            "SELECT count(*) FROM leads WHERE domain=? AND status IN ('sent','replied','bounced') AND id<>?",
            (lead["domain"], lead["id"])).fetchone()[0]
        if lead["domain"] and same_domain >= self.max_per_domain:
            raise SendBlocked("company already emailed")

    # -------------------------------------------------------------- smtp
    def _connect(self) -> smtplib.SMTP:
        host, port = self.cfg.env("SMTP_HOST", "smtp.gmail.com"), int(self.cfg.env("SMTP_PORT", "587"))
        user, pw = self.cfg.env("SMTP_USER"), self.cfg.env("SMTP_PASSWORD")
        if not (user and pw):
            raise SendBlocked("SMTP_USER / SMTP_PASSWORD not set in .env")
        ctx = ssl.create_default_context()
        if port == 465:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
        else:
            smtp = smtplib.SMTP(host, port, timeout=30)
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls(context=ctx)
                smtp.ehlo()
        smtp.login(user, pw)
        return smtp

    def build_message(self, lead) -> EmailMessage:
        s = self.cfg.sender
        msg = EmailMessage()
        msg["From"] = formataddr((s["name"], s["email"]))
        msg["To"] = lead["email"]
        msg["Subject"] = lead["email_subject"]
        msg["Reply-To"] = s["email"]
        msg["Message-ID"] = make_msgid(domain=s["email"].split("@")[-1] if "@" in s["email"] else None)
        msg["List-Unsubscribe"] = f"<mailto:{s['email']}?subject=unsubscribe>"
        msg.set_content(lead["email_body"])  # plain text only: better deliverability for 1:1 email
        return msg

    def test_connection(self) -> str:
        smtp = self._connect()
        smtp.quit()
        return "SMTP login OK"

    # --------------------------------------------------------------- run
    def run(self, ignore_window: bool = False, limit: int | None = None) -> dict:
        counts = {"sent": 0, "blocked": 0, "failed": 0, "dry_run": 0}
        if not ignore_window and not self.in_window():
            log.warning("Outside the send window %s; nothing sent. Use --ignore-window to override.", self.window)
            return counts
        statuses = ("approved",) if self.approval else ("approved", "drafted")
        queue = self.db.leads_by_status(*statuses)
        remaining = self.daily_limit - self.sent_last_24h()
        if limit is not None:
            remaining = min(remaining, limit)
        if remaining <= 0:
            log.warning("Daily limit reached (%d in the last 24h).", self.daily_limit)
            return counts
        log.info("%d emails queued, %d allowed now (daily limit %d)%s", len(queue), remaining, self.daily_limit,
                 " [DRY RUN]" if self.dry_run else "")

        try:
            for i, lead in enumerate(queue):
                if counts["sent"] + counts["dry_run"] >= remaining:
                    break
                try:
                    self.check(lead)
                except SendBlocked as e:
                    counts["blocked"] += 1
                    self.db.log(self.name, "blocked", lead["id"], str(e))
                    if str(e).startswith("suppressed"):
                        self.db.update_lead(lead["id"], status="suppressed", error=str(e))
                    log.info("Blocked %-30s %s", lead["company"][:30], e)
                    continue

                msg = self.build_message(lead)
                if self.dry_run:
                    counts["dry_run"] += 1
                    self.db.log(self.name, "dry_run", lead["id"], {"to": lead["email"], "subject": msg["Subject"]})
                    log.info("[DRY RUN] would send to %s: %s", lead["email"], msg["Subject"])
                    continue

                try:
                    if self._smtp is None:
                        self._smtp = self._connect()
                    self._smtp.send_message(msg)
                except smtplib.SMTPRecipientsRefused as e:
                    self.db.update_lead(lead["id"], status="bounced", error=str(e)[:300])
                    self.db.suppress(lead["email"], "email", "bounced")
                    self.db.log(self.name, "bounced", lead["id"], str(e)[:300])
                    counts["failed"] += 1
                    continue
                except (smtplib.SMTPException, OSError) as e:
                    self.db.update_lead(lead["id"], status="failed", error=str(e)[:300])
                    self.db.log(self.name, "failed", lead["id"], str(e)[:300])
                    counts["failed"] += 1
                    log.error("Send failed for %s: %s", lead["email"], e)
                    self._smtp = None
                    if isinstance(e, smtplib.SMTPAuthenticationError):
                        break
                    continue

                self.db.update_lead(lead["id"], status="sent", sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    message_id=msg["Message-ID"], error=None)
                self.db.suppress(lead["email"], "email", "emailed")
                self.db.log(self.name, "sent", lead["id"], {"to": lead["email"], "subject": msg["Subject"]})
                counts["sent"] += 1
                log.info("Sent to %s (%s)", lead["email"], lead["company"])

                if counts["sent"] < remaining and i < len(queue) - 1:
                    delay = random.uniform(self.min_delay, self.max_delay)
                    log.info("Waiting %.0fs before next send...", delay)
                    time.sleep(delay)
        finally:
            if self._smtp:
                try:
                    self._smtp.quit()
                except Exception:
                    pass
                self._smtp = None
        return counts
