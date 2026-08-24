"""Session tokens for hosted-service authentication.

A session binds a random customer_id to a random bearer token. Every
authenticated endpoint requires BOTH headers:
    X-Customer-ID: <uuid>
    X-Session-Token: <token>
A token is only valid for its own customer_id — cross-wallet access is
impossible by construction.
"""

from __future__ import annotations

import secrets
import threading
import uuid
from typing import Dict, Optional, Tuple


class SessionStore:
    """In-memory session store. Survives process lifetime only."""

    def __init__(self) -> None:
        self._sessions: Dict[str, str] = {}  # customer_id -> token
        self._lock = threading.Lock()

    def create(self) -> Tuple[str, str]:
        customer_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[customer_id] = token
        return customer_id, token

    def verify(self, customer_id: str, token: str) -> bool:
        if not customer_id or not token:
            return False
        expected = self._sessions.get(customer_id)
        if expected is None:
            return False
        return secrets.compare_digest(expected, token)
