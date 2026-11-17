"""Vercel entry point: the review page and /api/tick, served as one Python function.

Configuration comes from Vercel environment variables (DATABASE_URL, GEMINI_API_KEY,
SMTP_*/IMAP_* ...) and the repo's config/*.yaml.
"""
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RETRY_SECONDS = 10  # a failed startup is retried on the next request after this long


def _setup_page(exc: Exception):
    """Shown while the app can't start (e.g. DATABASE_URL missing, database briefly unavailable)."""
    from flask import Flask

    fallback = Flask(__name__)
    reason = ("The database isn't configured yet: add DATABASE_URL in Vercel → Settings → Environment "
              "Variables, then redeploy." if "DATABASE_URL" in str(exc) else
              "The app couldn't start. Check the function logs in Vercel for the error.")

    @fallback.route("/", defaults={"path": ""})
    @fallback.route("/<path:path>")
    def not_ready(path):
        return (f"<!doctype html><meta name=viewport content='width=device-width'><title>Setup needed</title>"
                f"<body style='font:16px system-ui;max-width:560px;margin:10vh auto;padding:0 16px'>"
                f"<h1>Almost there</h1><p>{reason}</p></body>", 503)

    return fallback


class _Dispatcher:
    """Serves the real app once it has started. If startup fails, the setup page is served and
    startup is retried on a later request, so a transient failure (two instances racing at cold
    start, a dropped database connection) doesn't keep this instance broken for its lifetime."""

    def __init__(self):
        self._app = self._fallback = None
        self._failed_at = 0.0
        self._lock = threading.Lock()
        self._build()

    def _build(self) -> None:
        try:
            from app.web import create_app

            self._app = create_app()
        except Exception as exc:  # show a setup page instead of a bare 500
            logging.exception("startup failed")
            self._failed_at = time.monotonic()
            self._fallback = _setup_page(exc)

    def __call__(self, environ, start_response):
        if self._app is None and time.monotonic() - self._failed_at >= RETRY_SECONDS:
            with self._lock:
                if self._app is None and time.monotonic() - self._failed_at >= RETRY_SECONDS:
                    self._build()
        return (self._app or self._fallback)(environ, start_response)


# Vercel imports `app`: keep it a Flask app and dispatch every request through the standard
# WSGI middleware hook.
from flask import Flask  # noqa: E402

app = Flask(__name__)
app.wsgi_app = _Dispatcher()
