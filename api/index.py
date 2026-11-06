"""Vercel entry point: the review page and /api/tick, served as one Python function.

Configuration comes from Vercel environment variables (DATABASE_URL, WEB_SECRET_KEY,
CRON_SECRET, GEMINI_API_KEY, SMTP_*/IMAP_* ...) and the repo's config/*.yaml.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.web import create_app  # noqa: E402

app = create_app()
