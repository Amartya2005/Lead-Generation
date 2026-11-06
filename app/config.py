"""Loads settings.yaml, portfolio.yaml, icp.yaml and .env into one Config object."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.getenv("LEADGEN_CONFIG_DIR", ROOT / "config"))


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Config:
    def __init__(self, env_file: str | os.PathLike | None = None):
        load_dotenv(env_file or ROOT / ".env", override=False)
        self.settings: dict[str, Any] = _load_yaml("settings.yaml")
        self.portfolio: list[dict] = _load_yaml("portfolio.yaml").get("projects", [])
        self.icp: dict[str, Any] = _load_yaml("icp.yaml")
        self._apply_env_overrides()
        self._validate_portfolio()

    # ------------------------------------------------------------------ env
    @staticmethod
    def env(name: str, default: str = "") -> str:
        return os.getenv(name, default).strip()

    def _apply_env_overrides(self) -> None:
        s = self.settings
        if self.env("LLM_PROVIDER"):
            s["llm"]["provider"] = self.env("LLM_PROVIDER").lower()
        if self.env("LLM_MODEL"):
            s["llm"]["model"] = self.env("LLM_MODEL")
        if self.env("DAILY_SEND_LIMIT"):
            s["sending"]["daily_limit"] = int(self.env("DAILY_SEND_LIMIT"))
        s["sending"]["human_approval"] = _bool(os.getenv("HUMAN_APPROVAL"), s["sending"].get("human_approval", True))
        s["sending"]["dry_run"] = _bool(os.getenv("DRY_RUN"), s["sending"].get("dry_run", False))

    def _validate_portfolio(self) -> None:
        if not self.portfolio:
            raise ValueError("config/portfolio.yaml has no projects. The Email Agent needs real work to reference.")
        for p in self.portfolio:
            for key in ("id", "domain", "url", "description", "capabilities"):
                if not p.get(key):
                    raise ValueError(f"portfolio project {p.get('id', '?')} is missing '{key}'")
            p.setdefault("confirmed", False)
            p.setdefault("kind", "client")
            p.setdefault("industries", [])

    # -------------------------------------------------------------- helpers
    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p

    def project(self, project_id: str) -> dict | None:
        return next((p for p in self.portfolio if p["id"] == project_id), None)

    @property
    def sender(self) -> dict[str, str]:
        return {
            "name": self.env("SENDER_NAME", "Your Name"),
            "company": self.env("SENDER_COMPANY", ""),
            "email": self.env("SENDER_EMAIL") or self.env("SMTP_USER"),
            "website": self.env("SENDER_WEBSITE", ""),
            "phone": self.env("SENDER_PHONE", ""),
        }

    @property
    def allowed_email_domains(self) -> set[str]:
        """Only these domains may appear as links in an email."""
        from urllib.parse import urlparse

        doms = {p["domain"].lower() for p in self.portfolio}
        site = self.sender["website"]
        if site:
            doms.add((urlparse(site if "://" in site else "https://" + site).hostname or "").lower())
        return {d.removeprefix("www.") for d in doms if d}


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    from rich.logging import RichHandler

    log_file = cfg.path("logs/run.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
        force=True,
    )
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    for noisy in ("urllib3", "httpx", "primp", "ddgs", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
