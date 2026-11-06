"""India location detection and tiering from icp.yaml."""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache

from app.config import Config

PIN_RE = re.compile(r"\b[1-8]\d{2}\s?\d{3}\b")
PHONE_IN_RE = re.compile(r"(?:\+91[\s-]?|\b0)?[6-9]\d{4}[\s-]?\d{5}\b")
FOREIGN_RE = re.compile(
    r"\b(USA|United States|United Kingdom|London|Dubai|UAE|Singapore|Australia|Canada|New York|California|Toronto)\b"
)


class Geo:
    def __init__(self, cfg: Config):
        icp = cfg.icp
        self.weights: dict[str, float] = icp["location_weights"]
        self.city_tier: dict[str, str] = {}
        self.canonical: dict[str, str] = {}
        for tier, names in icp["cities"].items():
            for n in names:
                self.city_tier[n.lower()] = tier
                self.canonical[n.lower()] = n
        self.odisha_states = [s.lower() for s in icp["states"]["odisha"]]
        self.states = [s for s in icp["states"]["other"]] + icp["states"]["odisha"]
        names = sorted(self.city_tier, key=len, reverse=True)
        self._city_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b", re.I)
        self._state_re = re.compile(r"\b(" + "|".join(re.escape(s) for s in self.states) + r")\b", re.I)

    def detect(self, text: str, hint: str | None = None) -> dict:
        """Return {city, state, tier, is_india, weight, location}."""
        text = text or ""
        cities = Counter(m.group(1).lower() for m in self._city_re.finditer(text))
        states = Counter(m.group(1).lower() for m in self._state_re.finditer(text))

        # A city that appears next to a PIN code is probably the address.
        for m in PIN_RE.finditer(text):
            window = text[max(0, m.start() - 120): m.end() + 40]
            for c in self._city_re.finditer(window):
                cities[c.group(1).lower()] += 3

        city = None
        if cities:
            city = cities.most_common(1)[0][0]
        elif hint and hint.lower() in self.city_tier:
            city = hint.lower()

        state = states.most_common(1)[0][0] if states else None
        tier = self.city_tier.get(city) if city else None
        if tier is None and state and state in self.odisha_states:
            tier = "odisha"

        indian_signals = bool(city or state or "+91" in text or PHONE_IN_RE.search(text) or re.search(r"\bIndia\b", text))
        foreign = len(FOREIGN_RE.findall(text))
        is_india = indian_signals or (foreign == 0 and bool(hint))
        if foreign >= 3 and not (city or state):
            is_india = False

        if not is_india:
            weight = 0.0
        elif tier:
            weight = float(self.weights.get(tier, 0))
        else:
            weight = float(self.weights.get("india_unknown", 0.5))
            tier = "india_unknown"

        pretty_city = self.canonical.get(city, city.title()) if city else None
        pretty_state = next((s for s in self.states if s.lower() == state), None) if state else None
        if pretty_city and tier == "odisha" and not pretty_state:
            pretty_state = "Odisha"
        loc = ", ".join(x for x in (pretty_city, pretty_state) if x) or ("India" if is_india else "Unknown")
        return {"city": pretty_city, "state": pretty_state, "tier": tier, "is_india": is_india,
                "weight": weight, "location": loc}

    def city_from_query(self, query: str) -> str | None:
        m = self._city_re.search(query or "")
        return self.canonical.get(m.group(1).lower()) if m else None


@lru_cache(maxsize=1)
def _cached(cfg_id: int, cfg: Config) -> Geo:  # pragma: no cover - tiny helper
    return Geo(cfg)


def get_geo(cfg: Config) -> Geo:
    return _cached(id(cfg), cfg)
