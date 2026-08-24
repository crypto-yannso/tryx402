"""Optional anonymous telemetry — install ping only.

No PII. No tracking across sites. One anonymous install-id stored locally.
Disable with TRYX402_NO_TELEMETRY=1 or TRYX402_TELEMETRY_URL="" (empty).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
import urllib.error

__all__ = ["_ping"]

_TELEMETRY_URL = os.environ.get("TRYX402_TELEMETRY_URL") or "https://tryx402.fly.dev/v1/telemetry"
_INSTALL_ID_FILE = os.path.expanduser("~/.tryx402_install_id")
_PING_TIMEOUT_S = 2


def _get_install_id() -> str:
    """Return a persistent anonymous install id (16 hex chars)."""
    try:
        if os.path.exists(_INSTALL_ID_FILE):
            return open(_INSTALL_ID_FILE, "r").read().strip()
        raw = os.urandom(16)
        install_id = hashlib.sha256(raw).hexdigest()[:16]
        try:
            with open(_INSTALL_ID_FILE, "w") as f:
                f.write(install_id)
        except OSError:
            pass  # best-effort; if we can't write, we just generate a fresh id next time
        return install_id
    except Exception:
        return "unknown"


def _ping() -> None:
    """Fire-and-forget telemetry ping. Never raises."""
    if os.environ.get("TRYX402_NO_TELEMETRY"):
        return
    if not _TELEMETRY_URL:
        return
    try:
        payload = {
            "install_id": _get_install_id(),
            "version": __import__("tryx402").__version__,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": sys.platform,
        }
        data = json.dumps(payload, separators=(",", ":")).encode()
        req = urllib.request.Request(
            _TELEMETRY_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=_PING_TIMEOUT_S)
    except Exception:
        # Silently ignore: no network, no telemetry endpoint, etc.
        pass
