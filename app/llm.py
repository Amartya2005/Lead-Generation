"""Minimal free-tier LLM client (plain HTTP, no SDK lock-in).

Providers:
  gemini - Google AI Studio free tier           (GEMINI_API_KEY)
  groq   - Groq free tier, OpenAI-compatible     (GROQ_API_KEY)
  xai    - xAI Grok, OpenAI-compatible           (XAI_API_KEY)
  ollama - local models, OpenAI-compatible       (no key)
  none   - disable; agents fall back to rules/templates

Every call is rate-limited (requests_per_minute) and capped per run
(max_calls_per_run). Any failure returns None so callers can fall back.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from app.config import Config

log = logging.getLogger("llm")

OPENAI_COMPAT = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "xai": ("https://api.x.ai/v1", "XAI_API_KEY"),
    "ollama": (None, None),
}


class LLM:
    def __init__(self, cfg: Config):
        c = cfg.settings["llm"]
        self.cfg = cfg
        self.provider = (c.get("provider") or "none").lower()
        self.model = c.get("model") or c.get("default_models", {}).get(self.provider, "")
        self.temperature = float(c.get("temperature", 0.4))
        self.timeout = float(c.get("timeout_seconds", 60))
        self.min_gap = 60.0 / max(1, int(c.get("requests_per_minute", 8)))
        self.max_calls = int(c.get("max_calls_per_run", 150))
        self.calls = 0
        self._last = 0.0
        self._disabled_reason: str | None = None

        if self.provider == "gemini" and not cfg.env("GEMINI_API_KEY"):
            self._disabled_reason = "GEMINI_API_KEY not set"
        elif self.provider in ("groq", "xai") and not cfg.env(OPENAI_COMPAT[self.provider][1]):
            self._disabled_reason = f"{OPENAI_COMPAT[self.provider][1]} not set"
        elif self.provider not in ("gemini", "groq", "xai", "ollama"):
            self._disabled_reason = "provider is 'none'"
        if self._disabled_reason:
            log.info("LLM disabled (%s); using rule-based agents.", self._disabled_reason)

    @property
    def enabled(self) -> bool:
        return self._disabled_reason is None and self.calls < self.max_calls

    def _pace(self) -> None:
        wait = self._last + self.min_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    # ------------------------------------------------------------ transport
    def _gemini(self, system: str, prompt: str, want_json: bool) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": self.temperature,
                                 **({"responseMimeType": "application/json"} if want_json else {})},
        }
        r = requests.post(url, json=body, timeout=self.timeout,
                          headers={"x-goog-api-key": self.cfg.env("GEMINI_API_KEY")})
        r.raise_for_status()
        cand = r.json().get("candidates") or []
        if not cand:
            raise ValueError(f"no candidates: {r.text[:200]}")
        return "".join(p.get("text", "") for p in cand[0].get("content", {}).get("parts", []))

    def _openai_compat(self, system: str, prompt: str, want_json: bool) -> str:
        base, key_name = OPENAI_COMPAT[self.provider]
        if self.provider == "ollama":
            base = self.cfg.env("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        headers = {"Authorization": f"Bearer {self.cfg.env(key_name)}"} if key_name else {}
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if want_json and self.provider != "ollama":
            body["response_format"] = {"type": "json_object"}
        r = requests.post(f"{base.rstrip('/')}/chat/completions", json=body, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    # ------------------------------------------------------------------ api
    def complete(self, system: str, prompt: str, want_json: bool = False) -> str | None:
        if not self.enabled:
            return None
        for attempt in range(3):
            self._pace()
            self.calls += 1
            try:
                if self.provider == "gemini":
                    return self._gemini(system, prompt, want_json)
                return self._openai_compat(system, prompt, want_json)
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code == 429 and attempt < 2:
                    wait = 30 * (attempt + 1)
                    log.warning("LLM rate-limited (429); waiting %ss", wait)
                    time.sleep(wait)
                    continue
                if code in (401, 403):
                    self._disabled_reason = f"auth failed ({code})"
                log.warning("LLM call failed: %s %s", code, (e.response.text[:200] if e.response is not None else ""))
                return None
            except Exception as e:
                log.warning("LLM call failed: %s", e)
                return None
        return None

    def json(self, system: str, prompt: str) -> dict | None:
        raw = self.complete(system, prompt + "\n\nRespond with a single JSON object only.", want_json=True)
        if not raw:
            return None
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        try:
            return json.loads(raw)
        except ValueError:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except ValueError:
                    pass
        log.warning("LLM returned non-JSON: %s", raw[:200])
        return None
