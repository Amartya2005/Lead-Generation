"""Sender Agent: sends approved emails over Gmail/Outlook SMTP with safety controls.

Each lead is sent by its assigned team member (config/team.yaml): from the
member's own mailbox if SMTP_PASSWORD_<ID> is set, otherwise from the shared
company mailbox with the member's name as display name.

Checks run immediately before each send (in this order):
  1. human approval: status must be 'approved' (or 'drafted' when approval is off)
  2. assigned member exists and is active
  3. matched portfolio project has confirmed: true and the member may cite it
  4. draft passes email_rules.validate() again (catches bad manual edits)
  5. address / domain not on the suppression list
  6. daily limit per mailbox (rolling 24h, counted from actual sends)
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

from app.agents.email_writer import check_draft
from app.config import Config
from app.db import DB

log = logging.getLogger("sender")


class SendBlocked(Exception):
    pass


class SenderAgent:
    name = "sender"

    def __init__(self, cfg: Config, db: DB, only_member: str | None = None):
        self.cfg, self.db = cfg, db
        s = cfg.settings["sending"]
        self.daily_limit = int(s.get("daily_limit", 25))
        self.min_delay, self.max_delay = float(s.get("min_delay_seconds", 90)), float(s.get("max_delay_seconds", 300))
        self.approval = bool(s.get("human_approval", True))
        self.dry_run = bool(s.get("dry_run", False))
        self.window = s.get("send_window", {})
        self.only_member = only_member
        self._smtp: dict[str, smtplib.SMTP] = {}

    # ------------------------------------------------------------ guards
    def sent_last_24h(self, mailbox_id: str | None = None) -> int:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        return self.db.sent_since(since, mailbox_id)

    def in_window(self) -> bool:
        if not self.window:
            return True
        now = datetime.now(ZoneInfo(self.window.get("timezone", "Asia/Kolkata")))
        return (now.weekday() in self.window.get("weekdays", range(7))
                and self.window.get("start_hour", 0) <= now.hour < self.window.get("end_hour", 24))

    def member_for(self, lead) -> dict:
        member = self.cfg.member(lead["sender_id"]) if lead["sender_id"] else self.cfg.member(None)
        if not member:
            raise SendBlocked(f"assigned team member '{lead['sender_id']}' is not in config/team.yaml")
        if not member.get("active", True):
            raise SendBlocked(f"team member '{member['id']}' is inactive")
        return member

    def check(self, lead) -> tuple[dict, dict]:
        """Raises SendBlocked, or returns (member, mailbox)."""
        if not lead["email"]:
            raise SendBlocked("no email address")
        if self.approval and lead["status"] != "approved":
            raise SendBlocked("not approved (human approval mode is on)")
        member = self.member_for(lead)
        proj = self.cfg.project(lead["matched_project"] or "")
        if not proj:
            raise SendBlocked("no matched portfolio project")
        if not proj.get("confirmed"):
            raise SendBlocked(f"portfolio project '{proj['id']}' is not confirmed in config/portfolio.yaml")
        problems = check_draft(self.cfg, {**dict(lead), "sender_id": member["id"]},
                               lead["email_subject"], lead["email_body"])
        if problems:
            raise SendBlocked("draft breaks email rules: " + "; ".join(problems))
        reason = self.db.is_suppressed(lead["email"], lead["domain"])
        if reason:
            raise SendBlocked(f"suppressed ({reason})")
        mailbox = self.cfg.mailbox(member)
        if not (mailbox["smtp_user"] and mailbox["smtp_password"]):
            raise SendBlocked(f"no SMTP credentials for mailbox '{mailbox['id']}' (see .env)")
        return member, mailbox

    # -------------------------------------------------------------- smtp
    @staticmethod
    def connect(mailbox: dict) -> smtplib.SMTP:
        ctx = ssl.create_default_context()
        if mailbox["smtp_port"] == 465:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(mailbox["smtp_host"], 465, context=ctx, timeout=30)
        else:
            smtp = smtplib.SMTP(mailbox["smtp_host"], mailbox["smtp_port"], timeout=30)
            smtp.ehlo()
            if smtp.has_extn("starttls"):
                smtp.starttls(context=ctx)
                smtp.ehlo()
        smtp.login(mailbox["smtp_user"], mailbox["smtp_password"])
        return smtp

    def build_message(self, lead, member: dict, mailbox: dict) -> EmailMessage:
        sender = mailbox["from_email"]
        msg = EmailMessage()
        msg["From"] = formataddr((self.cfg.from_name(member), sender))
        msg["To"] = lead["email"]
        msg["Subject"] = lead["email_subject"]
        # Replies go to the sending mailbox so `sync-inbox` sees them and suppresses the lead.
        msg["Reply-To"] = sender
        msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] if "@" in sender else None)
        msg["List-Unsubscribe"] = f"<mailto:{sender}?subject=unsubscribe>"
        msg.set_content(lead["email_body"])  # plain text only: better deliverability for 1:1 email
        return msg

    def test_connection(self, member_id: str | None = None) -> str:
        member = self.cfg.member(member_id)
        if not member:
            raise SendBlocked(f"unknown team member '{member_id}'")
        mb = self.cfg.mailbox(member)
        if not (mb["smtp_user"] and mb["smtp_password"]):
            raise SendBlocked(f"no SMTP credentials for mailbox '{mb['id']}' (see .env)")
        self.connect(mb).quit()
        return f"SMTP login OK for {mb['smtp_user']} ({mb['id']}), sending as \"{self.cfg.from_name(member)}\""

    def _smtp_for(self, mailbox: dict) -> smtplib.SMTP:
        if mailbox["id"] not in self._smtp:
            self._smtp[mailbox["id"]] = self.connect(mailbox)
        return self._smtp[mailbox["id"]]

    # --------------------------------------------------------------- run
    def run(self, ignore_window: bool = False, limit: int | None = None) -> dict:
        from app.db import AlreadyRunning

        try:
            with self.db.lock("send", ttl_seconds=4 * 3600):
                return self._run(ignore_window, limit)
        except AlreadyRunning:
            log.warning("Another send is already running; not starting a second one.")
            return {"sent": 0, "blocked": 0, "failed": 0, "dry_run": 0, "skipped": "already running"}

    def _queue(self) -> list:
        statuses = ("approved",) if self.approval else ("approved", "drafted")
        return [l for l in self.db.leads_by_status(*statuses)
                if not self.only_member or (l["sender_id"] or self.cfg.team["default_member"]) == self.only_member]

    def _attempt(self, lead_id: int, used: dict[str, int], dead: set[str]) -> str:
        """Try to send one lead. Returns 'sent' | 'dry_run' | 'blocked' | 'failed' | 'skipped'."""
        statuses = ("approved",) if self.approval else ("approved", "drafted")
        lead = self.db.get_lead(lead_id)  # re-read: a reviewer may have changed it meanwhile
        if not lead or lead["status"] not in statuses:
            return "skipped"
        try:
            member, mailbox = self.check(lead)
            if mailbox["id"] in dead:
                raise SendBlocked(f"mailbox '{mailbox['id']}' login failed earlier in this run")
            used.setdefault(mailbox["id"], self.sent_last_24h(mailbox["id"]))
            if used[mailbox["id"]] >= self.daily_limit:
                raise SendBlocked(f"daily limit reached for mailbox '{mailbox['id']}' ({self.daily_limit}/24h)")
        except SendBlocked as e:
            self.db.log(self.name, "blocked", lead["id"], str(e))
            if str(e).startswith("suppressed"):
                self.db.update_lead(lead["id"], status="suppressed", error=str(e))
            log.info("Blocked %-30s %s", lead["company"][:30], e)
            return "blocked"

        msg = self.build_message(lead, member, mailbox)
        if self.dry_run:
            used[mailbox["id"]] += 1
            self.db.log(self.name, "dry_run", lead["id"],
                        {"to": lead["email"], "from": msg["From"], "subject": msg["Subject"]})
            log.info("[DRY RUN] %s -> %s: %s", msg["From"], lead["email"], msg["Subject"])
            return "dry_run"

        # Claim it first, so a parallel sender (or a crash and retry) can never send it twice.
        if not self.db.claim_lead(lead["id"], lead["status"], "sending"):
            return "skipped"
        try:
            self._smtp_for(mailbox).send_message(msg)
        except smtplib.SMTPRecipientsRefused as e:
            self.db.update_lead(lead["id"], status="bounced", error=str(e)[:300])
            self.db.suppress(lead["email"], "email", "bounced")
            self.db.log(self.name, "bounced", lead["id"], str(e)[:300])
            return "failed"
        except (smtplib.SMTPException, OSError) as e:
            self.db.update_lead(lead["id"], status="failed", error=str(e)[:300])
            self.db.log(self.name, "failed", lead["id"], str(e)[:300])
            log.error("Send failed for %s via %s: %s", lead["email"], mailbox["id"], e)
            self._smtp.pop(mailbox["id"], None)
            if isinstance(e, smtplib.SMTPAuthenticationError):
                dead.add(mailbox["id"])
            return "failed"

        used[mailbox["id"]] += 1
        self.db.update_lead(lead["id"], status="sent", sent_from=mailbox["id"], sender_id=member["id"],
                            sent_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            message_id=msg["Message-ID"], error=None)
        self.db.suppress(lead["email"], "email", "emailed")
        self.db.log(self.name, "sent", lead["id"], {"to": lead["email"], "from": msg["From"], "subject": msg["Subject"]})
        log.info("Sent %s -> %s (%s)", msg["From"], lead["email"], lead["company"])
        return "sent"

    def _close(self) -> None:
        for smtp in self._smtp.values():
            try:
                smtp.quit()
            except Exception:
                pass
        self._smtp.clear()

    def _run(self, ignore_window: bool, limit: int | None) -> dict:
        """Long-running mode (VM / CLI): send the queue with random delays in between."""
        counts = {"sent": 0, "blocked": 0, "failed": 0, "dry_run": 0}
        if not ignore_window and not self.in_window():
            log.warning("Outside the send window %s; nothing sent. Use --ignore-window to override.", self.window)
            return counts
        queue = self._queue()
        budget = limit if limit is not None else len(queue)
        used: dict[str, int] = {}          # mailbox id -> sent in last 24h (incl. this run)
        dead: set[str] = set()             # mailboxes that failed to log in
        log.info("%d approved emails queued (daily limit %d per mailbox)%s", len(queue), self.daily_limit,
                 " [DRY RUN]" if self.dry_run else "")
        try:
            for i, queued in enumerate(queue):
                if counts["sent"] + counts["dry_run"] >= budget:
                    break
                outcome = self._attempt(queued["id"], used, dead)
                if outcome != "skipped":
                    counts[outcome] += 1
                if outcome == "sent" and counts["sent"] < budget and i < len(queue) - 1:
                    delay = random.uniform(self.min_delay, self.max_delay)
                    log.info("Waiting %.0fs before next send...", delay)
                    time.sleep(delay)
        finally:
            self._close()
        return counts

    def send_one(self, ignore_window: bool = False) -> dict:
        """Serverless mode: send at most ONE email, then return. The gap before the next
        send (random min..max delay) is stored in the database and enforced across calls."""
        from app.db import AlreadyRunning

        counts = {"sent": 0, "blocked": 0, "failed": 0, "dry_run": 0}
        if not ignore_window and not self.in_window():
            return {**counts, "skipped": "outside send window"}
        next_at = self.db.kv_get("next_send_at")
        now_utc = datetime.now(timezone.utc)
        if next_at and datetime.fromisoformat(next_at) > now_utc:
            return {**counts, "skipped": "waiting for random gap"}
        try:
            with self.db.lock("send", ttl_seconds=600):
                used: dict[str, int] = {}
                dead: set[str] = set()
                try:
                    for queued in self._queue():
                        outcome = self._attempt(queued["id"], used, dead)
                        if outcome != "skipped":
                            counts[outcome] += 1
                        if outcome in ("sent", "dry_run", "failed"):
                            gap = random.uniform(self.min_delay, self.max_delay)
                            self.db.kv_set("next_send_at", (datetime.now(timezone.utc) + timedelta(seconds=gap))
                                           .isoformat(timespec="seconds"))
                            break
                finally:
                    self._close()
        except AlreadyRunning:
            return {**counts, "skipped": "already running"}
        return counts

    def recover_stuck(self, minutes: int = 30) -> int:
        """A lead left in 'sending' (process killed mid-send) is marked failed, never retried,
        because we can't know whether the email went out. Check the Sent folder."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        rows = self.db.query("SELECT id FROM leads WHERE status='sending' AND updated_at<?", (cutoff,))
        for r in rows:
            self.db.update_lead(r["id"], status="failed",
                                error="interrupted while sending; check the Sent folder before retrying")
            self.db.log(self.name, "interrupted", r["id"])
        return len(rows)
