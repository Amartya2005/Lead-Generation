"""Shared fixtures: an isolated config copy (confirmed portfolio, two team members, no LLM)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """Config copy with confirmed portfolio, no LLM, fast crawl, no send window."""
    cdir = tmp_path / "config"
    shutil.copytree(ROOT / "config", cdir)
    port = yaml.safe_load((cdir / "portfolio.yaml").read_text())
    for p in port["projects"]:
        p["confirmed"] = True
    (cdir / "portfolio.yaml").write_text(yaml.safe_dump(port))
    s = yaml.safe_load((cdir / "settings.yaml").read_text())
    s["crawl"].update(per_host_delay_seconds=0, use_playwright_fallback=False, cache_hours=0)
    s["sending"].update(min_delay_seconds=0, max_delay_seconds=0, send_window={})
    s["storage"]["db_path"] = str(tmp_path / "leads.db")
    (cdir / "settings.yaml").write_text(yaml.safe_dump(s))
    team = yaml.safe_load((cdir / "team.yaml").read_text())
    team["members"].append({"id": "priya", "name": "Priya Das", "title": "Business Development",
                            "email": "priya@focustriggerautomation.com", "phone": "", "active": True})
    (cdir / "team.yaml").write_text(yaml.safe_dump(team))

    monkeypatch.setenv("LEADGEN_CONFIG_DIR", str(cdir))
    for k in [k for k in __import__("os").environ if k.startswith(("SMTP_", "IMAP_", "DEFAULT_SENDER"))]:
        monkeypatch.delenv(k)
    for k, v in {"LLM_PROVIDER": "none", "HUMAN_APPROVAL": "true", "DRY_RUN": "false", "DAILY_SEND_LIMIT": "25",
                 "SMTP_USER": "focustriggerautomation@gmail.com", "SMTP_PASSWORD": "app-pass"}.items():
        monkeypatch.setenv(k, v)
    import importlib

    import app.config as config_mod
    importlib.reload(config_mod)
    return config_mod.Config(env_file=tmp_path / "missing.env")


@pytest.fixture()
def db(cfg):
    from app.db import DB
    return DB(cfg.settings["storage"]["db_path"])


