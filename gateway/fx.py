"""Live FX rates with 24h disk cache. Falls back to the last cached rates,
then to the static stub, if the feed is unreachable — billing must never
break because an FX API hiccuped.

Feed: https://open.er-api.com/v6/latest/USD (free, no key, daily updates).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

CACHE_PATH = Path(os.environ.get("TRYX402_FX_CACHE",
                                 Path.home() / ".tryx402" / "fx_cache.json"))
MAX_AGE = 24 * 3600
FEED_URL = "https://open.er-api.com/v6/latest/USD"


def _fetch_live() -> dict | None:
    """Return {'USD':1.0,'EUR':0.92,...} from the live feed, or None."""
    try:
        with urllib.request.urlopen(FEED_URL, timeout=10) as r:
            data = json.loads(r.read().decode())
        rates = data.get("rates") or {}
        if "USD" not in rates:          # sanity: the feed is USD-based
            return None
        rates["USD"] = 1.0
        return {k.upper(): v for k, v in rates.items()
                if isinstance(v, (int, float))}
    except Exception:
        return None


@dataclass
class FxRates:
    """USD -> currency multipliers, refreshed at most once per 24h.
    Falls back to cache, then to the static stub."""
    fallback: dict = field(default_factory=lambda: {
        "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "CHF": 0.88,
        "CAD": 1.37, "AUD": 1.52, "JPY": 150.0, "INR": 83.0, "BRL": 5.0,
    })
    rates: dict = field(default_factory=dict)
    fetched_at: float = 0.0

    def __post_init__(self):
        self._load()

    def _load(self):
        """cache -> live (if stale) -> fallback."""
        try:
            c = json.loads(CACHE_PATH.read_text())
            self.rates, self.fetched_at = c["rates"], c["fetched_at"]
        except Exception:
            self.rates, self.fetched_at = {}, 0.0
        self.refresh(force=False)

    def refresh(self, force: bool = True) -> bool:
        """Refresh from the live feed if the cache is stale or forced.
        Returns True if we have usable rates afterwards."""
        if force or (time.time() - self.fetched_at) > MAX_AGE:
            live = _fetch_live()
            if live:
                self.rates, self.fetched_at = live, time.time()
                try:
                    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
                    CACHE_PATH.write_text(json.dumps(
                        {"rates": self.rates, "fetched_at": self.fetched_at}))
                except OSError:
                    pass
                return True
        # stale/failed: keep whatever we have; fall back if empty
        if not self.rates:
            self.rates = dict(self.fallback)
        return True

    @property
    def is_live(self) -> bool:
        return bool(self.fetched_at) and (time.time() - self.fetched_at) <= MAX_AGE

    def usd_to(self, currency: str) -> float:
        c = currency.upper()
        if c in self.rates:
            return self.rates[c]
        if c in self.fallback:
            return self.fallback[c]
        raise KeyError(f"no FX rate for {c}")
