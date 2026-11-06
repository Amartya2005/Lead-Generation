"""Web review page: log in as a team member, review / edit / approve drafts from a phone.

Run locally:   python main.py web            (http://127.0.0.1:8000, dev mode)
On a server:   gunicorn -w 2 -b 127.0.0.1:8000 'app.web:create_app()'   behind Caddy (HTTPS)

Security
  * Users are team members (config/team.yaml); passwords are hashed (werkzeug scrypt),
    created with `python main.py web-user add <member>`.
  * Every POST needs a per-session CSRF token. Sessions are signed, HttpOnly, SameSite=Lax,
    and Secure (HTTPS only) outside dev mode.
  * Repeated failed logins lock that username for a while.
  * Members can act on their own leads; admins on everyone's.
  * Approving re-runs the same email rules the Sender Agent enforces.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, abort, flash, g, redirect, render_template, request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from app.config import Config, _config_dirs
from app.db import DB, loads, now, open_db

log = logging.getLogger("web")
STATUS_TABS = ["drafted", "approved", "needs_contact", "sent", "replied", "bounced", "rejected", "discarded"]
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))  # equal-time check for unknown usernames


# --------------------------------------------------------------- config cache
class _ConfigCache:
    """Reload config when any YAML file changes, so team/portfolio edits need no restart."""

    def __init__(self):
        self._sig: tuple | None = None
        self._cfg: Config | None = None

    def _signature(self) -> tuple:
        base, local = _config_dirs()
        files = [p for d in (base, local) if d for p in sorted(Path(d).glob("*.yaml"))]
        return tuple((str(p), p.stat().st_mtime_ns) for p in files)

    def get(self) -> Config:
        sig = self._signature()
        if self._cfg is None or sig != self._sig:
            self._cfg, self._sig = Config(), sig
        return self._cfg


def _secret_key(cfg: Config, db: DB) -> str:
    """WEB_SECRET_KEY if set. Otherwise a random key generated once and stored with the
    data: in the database on Postgres (shared by all serverless instances), else in a
    file next to the SQLite database."""
    key = os.getenv("WEB_SECRET_KEY", "").strip()
    if key:
        return key
    if db.pg:
        db.execute("INSERT INTO kv(key, value, updated_at) VALUES ('web_secret', ?, ?) ON CONFLICT(key) DO NOTHING",
                   (secrets.token_hex(32), now()))
        return db.kv_get("web_secret")
    path = cfg.path(cfg.settings["storage"]["db_path"]).with_name(".web_secret")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    return path.read_text().strip()


def create_app(dev: bool = False, cfg: Config | None = None, db: DB | None = None) -> Flask:
    cache = _ConfigCache()
    cfg0 = cfg or cache.get()
    db = db or open_db(cfg0)
    web = cfg0.settings.get("web", {})

    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # behind Caddy
    app.config.update(
        SECRET_KEY=_secret_key(cfg0, db),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=not dev and os.getenv("WEB_COOKIE_SECURE", "true").lower() != "false",
        SESSION_COOKIE_NAME="leadgen_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=int(web.get("session_hours", 12))),
        MAX_CONTENT_LENGTH=256 * 1024,
    )

    def get_cfg() -> Config:
        return cfg if cfg is not None else cache.get()

    def setup_code() -> str | None:
        """One-time code for creating the first admin, only while no logins exist.
        Written to the server log (visible only to whoever runs the deployment)."""
        if db.scalar("SELECT count(*) FROM users"):
            return None
        db.execute("INSERT INTO kv(key, value, updated_at) VALUES ('setup_code', ?, ?) ON CONFLICT(key) DO NOTHING",
                   ("-".join(secrets.token_hex(3) for _ in range(3)), now()))
        return db.kv_get("setup_code") or None

    code = setup_code()
    if code:
        log.warning("No logins exist yet. SETUP CODE: %s  (open /setup on the site to create the first admin)", code)

    # ------------------------------------------------------------ helpers
    @app.before_request
    def load_user():
        g.cfg = get_cfg()
        g.db = db
        g.user = None
        g.nonce = secrets.token_urlsafe(16)
        username = session.get("user")
        if username:
            row = db.get_user(username)
            member = g.cfg.member(username) if row else None
            if row and member and member["id"] == username:
                g.user = {"username": username, "is_admin": bool(row["is_admin"]), "member": member,
                          "must_change": bool(row["must_change"])}
            else:
                session.clear()
        if (g.user and g.user["must_change"] and request.method == "GET"
                and request.endpoint not in ("account", "static", "healthz", "api_tick", "setup")):
            flash("Please choose your own password before continuing.", "warn")
            return redirect(url_for("account"))
        if request.method == "POST" and request.endpoint not in ("static",):
            sent = request.form.get("csrf", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Form expired. Go back, reload the page and try again.")

    @app.after_request
    def security_headers(resp):
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'nonce-{getattr(g, 'nonce', '')}'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.endpoint not in ("static", "healthz"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"user": g.get("user"), "csrf": session["csrf"], "company": g.cfg.company, "nonce": g.nonce,
                "loads": loads, "member_name": lambda mid: (g.cfg.member(mid) or {}).get("name", mid or "—")}

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.full_path if request.method == "GET" else None))
            return fn(*a, **kw)
        return wrapper

    def admin_required(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            if not g.user["is_admin"]:
                abort(403)
            return fn(*a, **kw)
        return wrapper

    def can_edit(lead) -> bool:
        return g.user["is_admin"] or lead["sender_id"] in (None, g.user["username"])

    def get_lead_or_404(lead_id: int, editable: bool = False):
        lead = db.get_lead(lead_id)
        if not lead:
            abort(404)
        if editable and not can_edit(lead):
            abort(403, "This lead is assigned to someone else. Ask them or an admin to reassign it.")
        return lead

    def actor() -> str:
        return f"web:{g.user['username']}"

    # ------------------------------------------------------------- routes
    @app.get("/healthz")
    def healthz():
        db.scalar("SELECT 1")
        return "ok", 200, {"Content-Type": "text/plain"}

    @app.get("/api/tick")
    def api_tick():
        """Scheduler entry point (GitHub Actions / Vercel Cron). Returns counts only.

        Without the CRON_SECRET bearer token a call is still accepted, but it can't force
        a tick: the database enforces the minimum spacing, so extra calls do nothing."""
        from app.tick import tick

        secret = os.getenv("CRON_SECRET", "")
        auth = request.headers.get("Authorization", "")
        authorized = bool(secret) and hmac.compare_digest(auth, f"Bearer {secret}")
        if os.getenv("TICK_REQUIRE_SECRET", "").lower() == "true" and not authorized:
            abort(401)
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        result = tick(g.cfg, db, force=authorized and request.args.get("force") == "1")
        return result, 200

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            max_fail, minutes = int(web.get("login_max_failures", 5)), int(web.get("lockout_minutes", 15))
            if db.recent_login_failures(username, minutes) >= max_fail:
                flash(f"Too many failed attempts. Try again in {minutes} minutes.", "error")
                return render_template("login.html"), 429
            row = db.get_user(username)
            ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password) and row is not None
            if ok and not g.cfg.member(username):
                ok = False
            db.record_login(username, ok, request.remote_addr)
            if not ok:
                flash("Wrong username or password.", "error")
                return render_template("login.html"), 401
            session.clear()
            session.permanent = True
            session["user"] = username
            session["csrf"] = secrets.token_urlsafe(32)
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        return render_template("login.html", first_run=not db.scalar("SELECT count(*) FROM users"))

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        from werkzeug.security import generate_password_hash

        expected = setup_code()
        if not expected:
            abort(404)
        if request.method == "POST":
            if db.recent_login_failures("__setup__", 15) >= 5:
                flash("Too many wrong codes. Try again in 15 minutes.", "error")
                return render_template("setup.html", members=g.cfg.active_members), 429
            member = g.cfg.member(request.form.get("member", ""))
            pw, repeat = request.form.get("password", ""), request.form.get("repeat", "")
            if not hmac.compare_digest(request.form.get("code", "").strip(), expected):
                db.record_login("__setup__", False, request.remote_addr)
                flash("Wrong setup code. It's in the server log.", "error")
            elif not member:
                flash("Pick a team member.", "error")
            elif len(pw) < 10 or pw != repeat:
                flash("Passwords must match and be at least 10 characters.", "error")
            else:
                db.upsert_user(member["id"], generate_password_hash(pw), is_admin=True)
                db.kv_set("setup_code", "")
                db.log(f"web:{member['id']}", "first_admin_created", None)
                session.clear()
                session.permanent = True
                session["user"] = member["id"]
                session["csrf"] = secrets.token_urlsafe(32)
                flash("Admin login created. Add the rest of the team under Logins.", "ok")
                return redirect(url_for("dashboard"))
            return redirect(url_for("setup"))
        return render_template("setup.html", members=g.cfg.active_members)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        from app.agents.sender import SenderAgent

        c = g.cfg
        counts = db.status_counts()
        mine = db.scalar("SELECT count(*) FROM leads WHERE status='drafted' AND sender_id=?",
                         (g.user["username"],))
        sender = SenderAgent(c, db)
        mailboxes = {}
        for m in c.members:
            mb = c.mailbox(m)
            mailboxes.setdefault(mb["id"], {"from": mb["from_email"], "sent": sender.sent_last_24h(mb["id"]),
                                            "creds": bool(mb["smtp_user"] and mb["smtp_password"]), "members": []})
            mailboxes[mb["id"]]["members"].append(m["name"])
        warnings = []
        if c.settings["sending"]["dry_run"]:
            warnings.append("DRY_RUN is on: approved emails are logged, not sent. Set DRY_RUN=false in .env when ready.")
        unconfirmed = [p["domain"] for p in c.portfolio if not p.get("confirmed")]
        if unconfirmed:
            warnings.append("Portfolio not confirmed (emails citing these won't send): " + ", ".join(unconfirmed))
        for mid, mb in mailboxes.items():
            if not mb["creds"]:
                warnings.append(f"No SMTP password for mailbox {mb['from']} ({mid}).")
        last = {k: db.last_event(*v) for k, v in {"research": ("research", "discovered"),
                                                  "draft": ("email", "drafted"), "send": ("sender", "sent")}.items()}
        return render_template("dashboard.html", counts=counts, mine=mine, mailboxes=mailboxes, warnings=warnings,
                               last=last, limit=sender.daily_limit, in_window=sender.in_window(),
                               window=c.settings["sending"].get("send_window", {}))

    @app.get("/leads")
    @login_required
    def leads():
        status = request.args.get("status", "drafted")
        who = request.args.get("who", "mine" if not g.user["is_admin"] else "all")
        q = request.args.get("q", "").strip()
        sql, args = "SELECT * FROM leads WHERE status=?", [status]
        if who == "mine":
            sql += " AND sender_id=?"
            args.append(g.user["username"])
        if q:
            sql += " AND (lower(company) LIKE ? OR lower(domain) LIKE ? OR lower(city) LIKE ?)"
            args += [f"%{q.lower()}%"] * 3
        sql += " ORDER BY score DESC NULLS LAST, id DESC LIMIT 200"
        rows = db.query(sql, args)
        return render_template("leads.html", rows=rows, status=status, who=who, q=q, tabs=STATUS_TABS,
                               counts=db.status_counts())

    @app.get("/leads/<int:lead_id>")
    @login_required
    def lead_detail(lead_id: int):
        lead = get_lead_or_404(lead_id)
        proj = g.cfg.project(lead["matched_project"] or "")
        from app.tools.email_rules import split_signature, word_count
        words = word_count(split_signature(lead["email_body"] or "")[0])
        return render_template("lead.html", lead=lead, proj=proj, signals=loads(lead["signals"], {}),
                               reasons=loads(lead["reasons"], []), needs=loads(lead["needs"], []),
                               problems=loads(lead["validation"], []), events=db.events_for(lead_id),
                               editable=can_edit(lead), members=g.cfg.active_members, words=words,
                               max_words=g.cfg.settings["email"].get("max_words", 120))

    def _save_draft(lead) -> list[str]:
        from app.agents.email_writer import check_draft

        subject = request.form.get("subject", lead["email_subject"] or "").strip()
        body = request.form.get("body", lead["email_body"] or "").replace("\r\n", "\n").strip()
        problems = check_draft(g.cfg, dict(lead), subject, body)
        if subject != lead["email_subject"] or body != lead["email_body"]:
            db.update_lead(lead["id"], email_subject=subject, email_body=body, validation=problems)
            db.log(actor(), "edited", lead["id"], {"problems": problems})
        return problems

    @app.post("/leads/<int:lead_id>/save")
    @login_required
    def lead_save(lead_id: int):
        lead = get_lead_or_404(lead_id, editable=True)
        if lead["status"] not in ("drafted", "needs_contact", "approved"):
            abort(409, f"Lead is {lead['status']}; it can't be edited.")
        problems = _save_draft(lead)
        if lead["status"] == "approved":  # edited after approval -> needs a fresh approval
            db.update_lead(lead_id, status="drafted")
        flash("Saved." + (" Fix the problems below before approving." if problems else ""),
              "warn" if problems else "ok")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/approve")
    @login_required
    def lead_approve(lead_id: int):
        lead = get_lead_or_404(lead_id, editable=True)
        if lead["status"] != "drafted":
            abort(409, f"Only drafts can be approved (this one is {lead['status']}).")
        if not lead["email"]:
            abort(409, "No email address for this lead.")
        problems = _save_draft(lead)
        if problems:
            flash("Not approved, the draft breaks the email rules: " + "; ".join(problems), "error")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        reason = db.is_suppressed(lead["email"], lead["domain"])
        if reason:
            db.update_lead(lead_id, status="suppressed")
            flash(f"Not approved, this address is suppressed ({reason}).", "error")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        db.update_lead(lead_id, status="approved", approved_at=now())
        db.log(actor(), "approved", lead_id)
        flash("Approved. It will go out in the next send run (within the send window).", "ok")
        return redirect(_next_draft_url(lead_id))

    @app.post("/leads/<int:lead_id>/reject")
    @login_required
    def lead_reject(lead_id: int):
        lead = get_lead_or_404(lead_id, editable=True)
        if lead["status"] not in ("drafted", "approved", "needs_contact"):
            abort(409)
        db.update_lead(lead_id, status="rejected")
        db.log(actor(), "rejected", lead_id, request.form.get("reason", "")[:200])
        flash("Rejected.", "ok")
        return redirect(_next_draft_url(lead_id))

    @app.post("/leads/<int:lead_id>/unapprove")
    @login_required
    def lead_unapprove(lead_id: int):
        lead = get_lead_or_404(lead_id, editable=True)
        if lead["status"] != "approved":
            abort(409)
        db.update_lead(lead_id, status="drafted")
        db.log(actor(), "unapproved", lead_id)
        flash("Moved back to drafts.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/assign")
    @login_required
    def lead_assign(lead_id: int):
        from app.graph import Agents
        from app.agents.email_writer import apply_signature, check_draft

        lead = get_lead_or_404(lead_id, editable=True)
        member = g.cfg.member(request.form.get("member", ""))
        if not member or member["id"] != request.form.get("member") or not member.get("active", True):
            abort(400, "Unknown or inactive team member.")
        if lead["status"] not in ("drafted", "approved", "needs_contact", "matched"):
            abort(409)
        proj = g.cfg.project(lead["matched_project"] or "")
        if proj and not g.cfg.project_allowed_for(proj, member["id"]):
            a = Agents(g.cfg, db)
            db.update_lead(lead_id, status="qualified")
            a.matching().match_one(db.get_lead(lead_id), member_id=member["id"])
            if db.get_lead(lead_id)["status"] == "matched":
                a.email().draft_one(db.get_lead(lead_id))
            a.close()
        else:
            body = apply_signature(g.cfg, lead["email_body"] or "", member) if lead["email_body"] else None
            fields = {"sender_id": member["id"], "status": "drafted" if lead["status"] == "approved" else lead["status"]}
            if body:
                fields.update(email_body=body, validation=check_draft(g.cfg, {**dict(lead), "sender_id": member["id"]},
                                                                      lead["email_subject"], body))
            db.update_lead(lead_id, **fields)
        db.log(actor(), "assigned", lead_id, member["id"])
        flash(f"Assigned to {member['name']}.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/redraft")
    @login_required
    def lead_redraft(lead_id: int):
        from app.graph import Agents

        lead = get_lead_or_404(lead_id, editable=True)
        if lead["status"] not in ("drafted", "needs_contact", "approved") or not lead["matched_project"]:
            abort(409)
        a = Agents(g.cfg, db)
        a.email().draft_one(lead)
        a.close()
        db.log(actor(), "redrafted", lead_id)
        flash("New draft written.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    def _next_draft_url(after_id: int) -> str:
        row = db.one(
            "SELECT id FROM leads WHERE status='drafted' AND id<>? AND (sender_id=? OR 1=?) "
            "ORDER BY score DESC NULLS LAST, id LIMIT 1",
            (after_id, g.user["username"], int(g.user["is_admin"])))
        return url_for("lead_detail", lead_id=row["id"]) if row else url_for("leads", status="drafted")

    @app.route("/suppression", methods=["GET", "POST"])
    @login_required
    def suppression():
        if request.method == "POST":
            value = request.form.get("value", "").strip().lower()
            if not value or " " in value or "." not in value:
                flash("Enter an email address or a domain.", "error")
            else:
                db.suppress(value, "email" if "@" in value else "domain", request.form.get("reason") or "manual")
                db.log(actor(), "suppressed", None, value)
                flash(f"{value} will never be emailed.", "ok")
            return redirect(url_for("suppression"))
        return render_template("suppression.html", rows=db.suppression_list()[:500])

    @app.get("/team")
    @login_required
    def team():
        c = g.cfg
        users = {u["username"]: u for u in db.list_users()}
        rows = []
        for m in c.members:
            mb = c.mailbox(m)
            rows.append({"m": m, "from": f"{c.from_name(m)} <{mb['from_email']}>", "mailbox": mb["id"],
                         "creds": bool(mb["smtp_user"] and mb["smtp_password"]), "user": users.get(m["id"])})
        return render_template("team.html", rows=rows, portfolio=c.portfolio, team=c.team,
                               on_vercel=bool(os.getenv("VERCEL")))

    @app.route("/account", methods=["GET", "POST"])
    @login_required
    def account():
        from werkzeug.security import generate_password_hash

        if request.method == "POST":
            row = db.get_user(g.user["username"])
            current, new, repeat = (request.form.get(k, "") for k in ("current", "new", "repeat"))
            if not check_password_hash(row["password_hash"], current):
                flash("Current password is wrong.", "error")
            elif len(new) < 10:
                flash("Use at least 10 characters.", "error")
            elif new != repeat:
                flash("The new passwords don't match.", "error")
            elif new == current:
                flash("Choose a password different from the current one.", "error")
            else:
                db.set_password(g.user["username"], generate_password_hash(new), must_change=False)
                db.log(actor(), "password_changed", None)
                flash("Password changed.", "ok")
                return redirect(url_for("dashboard"))
            return redirect(url_for("account"))
        return render_template("account.html")

    @app.route("/admin/users", methods=["GET", "POST"])
    @admin_required
    def admin_users():
        from werkzeug.security import generate_password_hash

        if request.method == "POST":
            action, username = request.form.get("action"), request.form.get("username", "")
            member = g.cfg.member(username)
            if not member or member["id"] != username:
                abort(400, "Not a member in team.yaml.")
            if action in ("create", "reset"):
                temp = "-".join(secrets.token_urlsafe(4) for _ in range(3))
                existing = db.get_user(username)
                is_admin = bool(request.form.get("is_admin")) if action == "create" else bool(existing and existing["is_admin"])
                db.upsert_user(username, generate_password_hash(temp), is_admin, must_change=True)
                db.log(actor(), f"user_{action}", None, username)
                flash(f"Temporary password for {member['name']}: {temp}  (share it privately; "
                      "they must change it at first sign-in)", "ok")
            elif action == "toggle_admin":
                if username == g.user["username"]:
                    abort(400, "You can't change your own admin role.")
                row = db.get_user(username)
                if row:
                    db.upsert_user(username, row["password_hash"], not row["is_admin"], bool(row["must_change"]))
                    db.log(actor(), "user_role", None, username)
            elif action == "remove":
                if username == g.user["username"]:
                    abort(400, "You can't remove your own login.")
                db.delete_user(username)
                db.log(actor(), "user_removed", None, username)
                flash("Login removed.", "ok")
            return redirect(url_for("admin_users"))
        users = {u["username"]: u for u in db.list_users()}
        return render_template("users.html", members=g.cfg.members, users=users)

    @app.post("/admin/check")
    @admin_required
    def admin_check():
        """Test each external connection and report OK / the error type. Never shows secrets."""
        import imaplib

        from app.agents.sender import SenderAgent
        from app.llm import LLM

        results = []

        def run(name, fn):
            try:
                results.append((name, True, fn()))
            except Exception as e:
                msg = str(e).replace("\n", " ")[:160]
                results.append((name, False, f"{type(e).__name__}: {msg}"))

        run("Database", lambda: f"{db.backend} OK, {db.scalar('SELECT count(*) FROM leads')} leads")
        seen = set()
        for m in g.cfg.active_members:
            mb = g.cfg.mailbox(m)
            if mb["id"] in seen:
                continue
            seen.add(mb["id"])
            run(f"Gmail sending ({mb['smtp_user'] or 'not set'})", lambda m=m: SenderAgent(g.cfg, db).test_connection(m["id"]))

            def imap_check(mb=mb):
                if not (mb["imap_user"] and mb["imap_password"]):
                    raise RuntimeError("IMAP_USER / IMAP_PASSWORD not set")
                with imaplib.IMAP4_SSL(mb["imap_host"], timeout=20) as M:
                    M.login(mb["imap_user"], mb["imap_password"])
                    M.select("INBOX", readonly=True)
                return "IMAP login OK (read-only)"
            run(f"Gmail inbox ({mb['imap_user'] or 'not set'})", imap_check)

        def llm_check():
            llm = LLM(g.cfg)
            if not llm.enabled:
                raise RuntimeError("LLM disabled (no key or provider 'none'); emails will use the template")
            out = llm.json("You are a connectivity test.", 'Return {"ok": true}')
            if not out:
                raise RuntimeError("no valid reply (see function logs)")
            return f"{llm.provider} / {llm.model} OK"
        run("Gemini (LLM)", llm_check)
        db.log(actor(), "connection_check", None, {n: ok for n, ok, _ in results})
        for name, ok, detail in results:
            flash(f"{'✓' if ok else '✗'} {name}: {detail}", "ok" if ok else "error")
        return redirect(url_for("team"))

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    def error(e):
        return render_template("error.html", error=e), e.code

    @app.template_filter("ago")
    def ago(ts: str | None) -> str:
        if not ts:
            return "never"
        try:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
        except ValueError:
            return ts
        mins = int(delta.total_seconds() // 60)
        if mins < 60:
            return f"{mins} min ago"
        if mins < 48 * 60:
            return f"{mins // 60} h ago"
        return f"{mins // 1440} days ago"

    return app
