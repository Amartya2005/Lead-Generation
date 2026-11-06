"""Read-only IMAP scan: marks leads as replied / bounced and suppresses them.

Opens INBOX read-only, so nothing in your mailbox is changed (not even the "seen" flag).
"""
from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timedelta

from app.config import Config
from app.db import DB

log = logging.getLogger("imap")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _body_text(msg: email.message.Message) -> str:
    parts = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "message/delivery-status", "text/rfc822-headers"):
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(errors="ignore"))
    return "\n".join(parts)


def _mailboxes(cfg: Config) -> list[dict]:
    """Shared company mailbox plus every member who sends from their own mailbox."""
    seen, out = set(), []
    for m in cfg.members:
        mb = cfg.mailbox(m)
        if mb["id"] not in seen:
            seen.add(mb["id"])
            out.append(mb)
    return out


def _sync_one(db: DB, mb: dict, sent: dict, since: str) -> tuple[int, int]:
    replied = bounced = 0
    with imaplib.IMAP4_SSL(mb["imap_host"]) as M:
        M.login(mb["imap_user"], mb["imap_password"])
        M.select("INBOX", readonly=True)

        # Replies: any message FROM a lead we emailed from this mailbox.
        for addr, lead in sent.items():
            typ, data = M.search(None, "SINCE", since, "FROM", f'"{addr}"')
            if typ == "OK" and data and data[0].split():
                db.update_lead(lead["id"], status="replied")
                db.suppress(addr, "email", "replied")
                db.log("inbox", "replied", lead["id"], {"from": addr, "mailbox": mb["id"]})
                replied += 1

        # Bounces: delivery-failure notices that mention a lead's address.
        typ, data = M.search(None, "SINCE", since, "OR", "FROM", '"mailer-daemon"', "FROM", '"postmaster"')
        for num in (data[0].split() if typ == "OK" and data else []):
            typ, msg_data = M.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            for addr in {a.lower() for a in EMAIL_RE.findall(_body_text(msg))}:
                lead = sent.pop(addr, None)
                if lead:
                    db.update_lead(lead["id"], status="bounced")
                    db.suppress(addr, "email", "bounced")
                    db.log("inbox", "bounced", lead["id"], {"to": addr, "mailbox": mb["id"]})
                    bounced += 1
    return replied, bounced


def sync_inbox(cfg: Config, db: DB, days: int = 30) -> dict:
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    totals = {"replied": 0, "bounced": 0}
    for mb in _mailboxes(cfg):
        sent = {r["email"].lower(): r for r in db.all_leads()
                if r["email"] and r["status"] in ("sent", "failed") and (r["sent_from"] or "shared") == mb["id"]}
        if not sent:
            continue
        if not (mb["imap_host"] and mb["imap_user"] and mb["imap_password"]):
            log.warning("No IMAP credentials for mailbox '%s'; skipping its inbox sync.", mb["id"])
            continue
        try:
            r, b = _sync_one(db, mb, sent, since)
        except (imaplib.IMAP4.error, OSError) as e:
            log.error("Inbox sync failed for %s: %s", mb["imap_user"], e)
            continue
        totals["replied"] += r
        totals["bounced"] += b
        log.info("Inbox %s: %d replied, %d bounced", mb["imap_user"], r, b)
    return totals
