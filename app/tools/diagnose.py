"""Step-by-step mail connection diagnostics (no secrets in the output).

Answers "where exactly does it fail?": DNS -> TCP -> server greeting -> STARTTLS/SSL -> login.
Used by the admin "Check connections" button and on demand by a tick (kv 'diag_requested').
"""
from __future__ import annotations

import imaplib
import smtplib
import socket
import ssl
import time


def _tcp_probe(host: str, port: int, timeout: float = 10) -> str:
    t0 = time.monotonic()
    try:
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError as e:
        return f"DNS failed: {e}"
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            ms = int((time.monotonic() - t0) * 1000)
            if port in (465, 993):
                return f"TCP ok to {ips[:2]} in {ms} ms (TLS port, no plain greeting expected)"
            s.settimeout(timeout)
            try:
                banner = s.recv(200).decode(errors="replace").strip()
            except socket.timeout:
                return f"TCP ok in {ms} ms but no greeting within {timeout:.0f}s"
            return f"TCP ok in {ms} ms, greeting: {banner[:90]!r}" if banner else f"TCP ok in {ms} ms, then closed with no greeting"
    except OSError as e:
        return f"TCP connect to {ips[:2]} failed: {type(e).__name__}: {e}"


def smtp_steps(host: str, port: int, user: str, password: str, timeout: float = 20) -> list[str]:
    out = [f"{host}:{port} {_tcp_probe(host, port)}"]
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=timeout)
            out.append("SSL handshake ok")
        else:
            s = smtplib.SMTP(timeout=timeout)
            code, msg = s.connect(host, port)
            out.append(f"greeting {code}")
            s.ehlo()
            if not s.has_extn("starttls"):
                out.append("server offers no STARTTLS")
                return out
            s.starttls(context=ctx)
            s.ehlo()
            out.append("STARTTLS ok")
        if not (user and password):
            out.append("login skipped: no username/password configured")
        else:
            try:
                s.login(user, password)
                out.append("LOGIN OK")
            except smtplib.SMTPAuthenticationError as e:
                out.append(f"login rejected {e.smtp_code}: {e.smtp_error.decode(errors='replace')[:140]}")
        try:
            s.quit()
        except Exception:
            pass
    except Exception as e:
        out.append(f"failed: {type(e).__name__}: {str(e)[:160]}")
    return out


def imap_steps(host: str, user: str, password: str, timeout: float = 20) -> list[str]:
    out = [f"{host}:993 {_tcp_probe(host, 993)}"]
    try:
        with imaplib.IMAP4_SSL(host, timeout=timeout) as M:
            out.append("SSL + greeting ok")
            if user and password:
                try:
                    M.login(user, password)
                    out.append("LOGIN OK")
                except imaplib.IMAP4.error as e:
                    out.append(f"login rejected: {str(e)[:140]}")
            else:
                out.append("login skipped: no username/password configured")
    except Exception as e:
        out.append(f"failed: {type(e).__name__}: {str(e)[:160]}")
    return out


def mail_report(cfg) -> dict:
    """Diagnose every mailbox in use, plus 465 as an alternative to 587."""
    report: dict[str, list[str]] = {}
    seen = set()
    for m in cfg.active_members:
        mb = cfg.mailbox(m)
        if mb["id"] in seen:
            continue
        seen.add(mb["id"])
        pw_note = f"password set ({len(mb['smtp_password'].replace(' ', ''))} chars)" if mb["smtp_password"] else "NO password set"
        report[f"{mb['id']} config"] = [f"user={mb['smtp_user'] or '-'} host={mb['smtp_host']}:{mb['smtp_port']} {pw_note}"]
        report[f"{mb['id']} smtp:{mb['smtp_port']}"] = smtp_steps(mb["smtp_host"], mb["smtp_port"], mb["smtp_user"], mb["smtp_password"])
        alt = 465 if mb["smtp_port"] != 465 else 587
        report[f"{mb['id']} smtp:{alt}"] = smtp_steps(mb["smtp_host"], alt, mb["smtp_user"], mb["smtp_password"])
        report[f"{mb['id']} imap"] = imap_steps(mb["imap_host"], mb["imap_user"], mb["imap_password"])
    # Is outbound mail blocked in general, or only towards Gmail? (greeting only, no login)
    report["control smtp-mail.outlook.com:587"] = [_tcp_probe("smtp-mail.outlook.com", 587)]
    return report
