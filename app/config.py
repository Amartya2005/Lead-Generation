"""Loads settings.yaml, portfolio.yaml, icp.yaml, team.yaml and .env into one Config object.

Layout on a server (all optional, see deploy/oracle/):
  LEADGEN_ENV_FILE          path to .env                      (default: <repo>/.env)
  LEADGEN_CONFIG_DIR        base config dir                   (default: <repo>/config)
  LEADGEN_LOCAL_CONFIG_DIR  server-specific overrides: any YAML file here is deep-merged
                            over the base one (dicts merge, lists replace), so `git pull`
                            never touches your team, portfolio or limits.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _config_dirs() -> tuple[Path, Path | None]:
    base = Path(os.getenv("LEADGEN_CONFIG_DIR") or ROOT / "config")
    local = os.getenv("LEADGEN_LOCAL_CONFIG_DIR")
    return base, (Path(local) if local else None)


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = deep_merge(base.get(k), v) if k in base else v
        return out
    return override


def _load_yaml(name: str) -> dict:
    base_dir, local_dir = _config_dirs()
    with open(base_dir / name, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if local_dir and (local_dir / name).exists():
        with open(local_dir / name, encoding="utf-8") as f:
            data = deep_merge(data, yaml.safe_load(f) or {})
    return data


class Config:
    def __init__(self, env_file: str | os.PathLike | None = None):
        load_dotenv(env_file or os.getenv("LEADGEN_ENV_FILE") or ROOT / ".env", override=False)
        self.settings: dict[str, Any] = _load_yaml("settings.yaml")
        self.portfolio: list[dict] = _load_yaml("portfolio.yaml").get("projects", [])
        self.icp: dict[str, Any] = _load_yaml("icp.yaml")
        self.team: dict[str, Any] = _load_yaml("team.yaml")
        self._apply_env_overrides()
        self._validate_portfolio()
        self._validate_team()

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
        if self.env("DEFAULT_SENDER"):
            self.team["default_member"] = self.env("DEFAULT_SENDER")

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
            p.setdefault("owner", None)  # personal portfolio: only its owner may cite it

    def _validate_team(self) -> None:
        c = self.team.setdefault("company", {})
        for key in ("legal_name", "short_name", "website"):
            if not c.get(key):
                raise ValueError(f"config/team.yaml: company.{key} is required")
        c.setdefault("email", "")
        c.setdefault("phone", "")
        members = self.team.setdefault("members", [])
        if not members:
            raise ValueError("config/team.yaml needs at least one member (who signs the emails)")
        ids = set()
        for m in members:
            if not m.get("id") or not m.get("name"):
                raise ValueError("every team member needs an id and a name")
            if not re.fullmatch(r"[a-z0-9_]+", m["id"]):
                raise ValueError(f"member id '{m['id']}' must be lowercase letters, digits or _")
            if m["id"] in ids:
                raise ValueError(f"duplicate member id '{m['id']}'")
            ids.add(m["id"])
            for k in ("title", "email", "phone"):
                m[k] = m.get(k) or ""
            m.setdefault("active", True)
        if self.team.get("default_member") not in ids:
            self.team["default_member"] = members[0]["id"]
        for p in self.portfolio:
            if p["owner"] and p["owner"] not in ids:
                raise ValueError(f"portfolio project {p['id']}: owner '{p['owner']}' is not a member in team.yaml")

    # -------------------------------------------------------------- helpers
    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ROOT / p

    def project(self, project_id: str) -> dict | None:
        return next((p for p in self.portfolio if p["id"] == project_id), None)

    # ----------------------------------------------------------------- team
    @property
    def company(self) -> dict[str, str]:
        return self.team["company"]

    @property
    def members(self) -> list[dict]:
        return self.team["members"]

    @property
    def active_members(self) -> list[dict]:
        return [m for m in self.members if m.get("active", True)]

    def member(self, member_id: str | None) -> dict | None:
        return next((m for m in self.members if m["id"] == (member_id or self.team["default_member"])), None)

    def from_name(self, member: dict) -> str:
        c = self.company
        return self.team.get("from_name_format", "{name} | {company}").format(
            name=member["name"], company=c["short_name"], legal_name=c["legal_name"])

    @staticmethod
    def _clean_password(pw: str, host: str) -> str:
        """Copy-paste fixes: surrounding quotes/whitespace always; for Gmail also every inner space
        (including non-breaking ones), since App Passwords are 16 letters shown in groups of four."""
        pw = (pw or "").strip().strip("'\"").strip()
        return re.sub(r"\s", "", pw) if "gmail.com" in (host or "") else pw

    def mailbox(self, member: dict) -> dict[str, Any]:
        """SMTP/IMAP settings for a member: their own mailbox if SMTP_PASSWORD_<ID> is set,
        otherwise the shared company mailbox (SMTP_USER / SMTP_PASSWORD)."""
        key = member["id"].upper()
        own_pw = self.env(f"SMTP_PASSWORD_{key}")
        if own_pw:
            user = self.env(f"SMTP_USER_{key}") or member["email"]
            mb = {
                "id": f"member:{member['id']}", "own": True, "from_email": member["email"] or user,
                "smtp_host": self.env(f"SMTP_HOST_{key}") or self.env("SMTP_HOST", "smtp.gmail.com"),
                "smtp_port": int(self.env(f"SMTP_PORT_{key}") or self.env("SMTP_PORT", "587")),
                "smtp_user": user, "smtp_password": own_pw,
                "imap_host": self.env(f"IMAP_HOST_{key}") or self.env("IMAP_HOST", "imap.gmail.com"),
                "imap_user": user, "imap_password": self.env(f"IMAP_PASSWORD_{key}") or own_pw,
            }
            return self._cleaned(mb)
        user = self.env("SMTP_USER") or self.company["email"]
        return self._cleaned({
            "id": "shared", "own": False, "from_email": user,
            "smtp_host": self.env("SMTP_HOST", "smtp.gmail.com"), "smtp_port": int(self.env("SMTP_PORT", "587")),
            "smtp_user": user, "smtp_password": self.env("SMTP_PASSWORD"),
            "imap_host": self.env("IMAP_HOST", "imap.gmail.com"), "imap_user": self.env("IMAP_USER") or user,
            "imap_password": self.env("IMAP_PASSWORD") or self.env("SMTP_PASSWORD"),
        })

    def _cleaned(self, mb: dict) -> dict:
        mb["smtp_password"] = self._clean_password(mb["smtp_password"], mb["smtp_host"])
        mb["imap_password"] = self._clean_password(mb["imap_password"], mb["imap_host"])
        return mb

    def project_allowed_for(self, project: dict, member_id: str | None) -> bool:
        """A personal portfolio (owner set) may only be cited by its owner."""
        return not project.get("owner") or project["owner"] == (member_id or self.team["default_member"])

    @property
    def allowed_email_domains(self) -> set[str]:
        """Only these domains may appear as links in an email."""
        from urllib.parse import urlparse

        doms = {p["domain"].lower() for p in self.portfolio}
        site = self.company["website"]
        if site:
            doms.add((urlparse(site if "://" in site else "https://" + site).hostname or "").lower())
        return {d.removeprefix("www.") for d in doms if d}


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    from rich.logging import RichHandler

    log_file = cfg.path(cfg.settings.get("storage", {}).get("log_file", "logs/run.log"))
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
