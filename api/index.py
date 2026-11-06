"""Vercel entry point: the review page and /api/tick, served as one Python function.

Configuration comes from Vercel environment variables (DATABASE_URL, GEMINI_API_KEY,
SMTP_*/IMAP_* ...) and the repo's config/*.yaml.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app.web import create_app  # noqa: E402

    app = create_app()
except Exception as exc:  # show a setup page instead of a bare 500 while configuration is incomplete
    logging.exception("startup failed")
    from flask import Flask

    app = Flask(__name__)
    reason = ("The database isn't configured yet: add DATABASE_URL in Vercel → Settings → Environment Variables, "
              "then redeploy." if "DATABASE_URL" in str(exc) else
              "The app couldn't start. Check the function logs in Vercel for the error.")

    @app.route("/", defaults={"path": ""})
    @app.route("/<path:path>")
    def not_ready(path):
        return (f"<!doctype html><meta name=viewport content='width=device-width'><title>Setup needed</title>"
                f"<body style='font:16px system-ui;max-width:560px;margin:10vh auto;padding:0 16px'>"
                f"<h1>Almost there</h1><p>{reason}</p></body>", 503)
