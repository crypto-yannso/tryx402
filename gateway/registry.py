"""Server-side price registry for the hosted proxy.

The price of an endpoint is decided HERE, never by the client. An origin
that is not registered cannot be proxied at all — this closes both the
client-declared-price hole and SSRF (only registered public origins are
reachable through /v1/proxy/call).
"""

from __future__ import annotations

import ipaddress
import threading
from typing import Dict
from urllib.parse import urlparse

__all__ = ["PriceRegistry", "UnknownOriginError", "PrivateOriginError",
           "seed_from_tools_db"]


def seed_from_tools_db(db_path: str, active_only: bool = True) -> "PriceRegistry":
    """Build a PriceRegistry pre-loaded from the private repo's tools DB."""
    reg = PriceRegistry()
    reg.seed_from_tools_db(db_path, active_only=active_only)
    return reg


class UnknownOriginError(Exception):
    pass


class PrivateOriginError(Exception):
    pass


def _is_private_origin(origin: str) -> bool:
    parsed = urlparse(origin)
    host = parsed.hostname or ""
    # Literal IPs
    try:
        addr = ipaddress.ip_address(host)
        return not addr.is_global
    except ValueError:
        pass
    # Hostnames: localhost family and bare/internal names
    lowered = host.lower()
    if lowered in ("localhost",) or lowered.endswith(".local") or lowered.endswith(".internal"):
        return True
    if lowered in ("metadata.google.internal",):
        return True
    return False


class PriceRegistry:
    def __init__(self) -> None:
        self._prices: Dict[str, int] = {}  # origin -> price_cents
        self._allow_private: set = set()
        self._lock = threading.Lock()

    def register(self, origin: str, price_cents: int,
                 allow_private: bool = False) -> None:
        origin = origin.rstrip("/")
        if price_cents < 0:
            raise ValueError("price_cents must be non-negative")
        with self._lock:
            self._prices[origin] = int(price_cents)
            if allow_private:
                self._allow_private.add(origin)

    def seed_from_tools_db(self, db_path: str, active_only: bool = True) -> int:
        """Load origins/prices from the private repo's tools table.

        Returns the number of origins registered. Inactive tools are
        skipped (they have not passed a paid health-check).
        """
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            query = "SELECT origin, price_usd, is_active FROM tools"
            rows = conn.execute(query).fetchall()
        finally:
            conn.close()
        count = 0
        for origin, price_usd, is_active in rows:
            if active_only and not is_active:
                continue
            if not origin or price_usd is None:
                continue
            self.register(origin=origin,
                          price_cents=max(1, round(float(price_usd) * 100)))
            count += 1
        return count

    def lookup(self, url: str) -> int:
        """Return the server-set price in cents for a full URL.

        Raises UnknownOriginError if the origin is not registered.
        Raises PrivateOriginError if origin is private and not explicitly allowed.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise PrivateOriginError(f"scheme not allowed: {parsed.scheme!r}")
        origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        with self._lock:
            if origin not in self._prices:
                raise UnknownOriginError(f"origin not registered: {origin}")
            if origin not in self._allow_private and _is_private_origin(origin):
                raise PrivateOriginError(f"private origin blocked: {origin}")
            return self._prices[origin]
