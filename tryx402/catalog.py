"""Discovery over the tryx402 catalogue — thin client calling the public API.

Queries https://tryx402.fly.dev/api/v1/tools for semantic search and provider discovery.
Zero local files, zero database dependencies.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List

DEFAULT_API_BASE = os.environ.get("TRYX402_API_BASE", "https://tryx402.fly.dev").rstrip("/")


def search(query: str, binary: Any = None, limit: int = 10) -> List[Dict[str, Any]]:
    """Find endpoints by intent using the server-side TF-IDF index."""
    q = urllib.parse.quote(query)
    url = f"{DEFAULT_API_BASE}/api/v1/tools/search?query={q}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "tryx402-python/0.4.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("results", [])
    except Exception:
        return []


def discover(origin: str) -> List[Dict[str, Any]]:
    """Introspect an origin using the server-side catalog API."""
    orig = urllib.parse.quote(origin)
    url = f"{DEFAULT_API_BASE}/api/v1/tools/discover?origin={orig}"
    req = urllib.request.Request(url, headers={"User-Agent": "tryx402-python/0.4.1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("results", data.get("tools", data.get("endpoints", [])))
    except Exception:
        return []
