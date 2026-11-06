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


def sync_inbox(cfg: Config, db: DB, days: int = 30) -> dict:
    host, user, pw = cfg.env("IMAP_HOST"), cfg.env("IMAP_USER"), cfg.env("IMAP_PASSWORD")
    if not (host and user and pw):
        log.warning("IMAP_HOST / IMAP_USER / IMAP_PASSWORD not set; skipping inbox sync.")
        return {"replied": 0, "bounced": 0}

    sent = {r["email"].lower(): r for r in db.all_leads() if r["email"] and r["status"] in ("sent", "failed")}
    if not sent:
        return {"replied": 0, "bounced": 0}
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    replied = bounced = 0

    with imaplib.IMAP4_SSL(host) as M:
        M.login(user, pw)
        M.select("INBOX", readonly=True)

        # Replies: any message FROM a lead we emailed.
        for addr, lead in sent.items():
            typ, data = M.search(None, "SINCE", since, "FROM", f'"{addr}"')
            if typ == "OK" and data and data[0].split():
                db.update_lead(lead["id"], status="replied")
                db.suppress(addr, "email", "replied")
                db.log("inbox", "replied", lead["id"], addr)
                replied += 1

        # Bounces: delivery-failure notices that mention a lead's address.
        typ, data = M.search(None, "SINCE", since, "OR", "FROM", '"mailer-daemon"', "FROM", '"postmaster"')
        for num in (data[0].split() if typ == "OK" and data else []):
            typ, msg_data = M.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            for addr in {a.lower() for a in EMAIL_RE.findall(_body_text(msg))}:
                lead = sent.get(addr)
                if lead and lead["status"] != "bounced":
                    db.update_lead(lead["id"], status="bounced")
                    db.suppress(addr, "email", "bounced")
                    db.log("inbox", "bounced", lead["id"], addr)
                    bounced += 1
    log.info("Inbox sync: %d replied, %d bounced", replied, bounced)
    return {"replied": replied, "bounced": bounced}
