"""Session tokens for hosted-service authentication.

A session binds a customer_id to a random bearer token. Every
authenticated endpoint requires BOTH headers:
    X-Customer-ID: <id>
    X-Session-Token: <token>
A token is only valid for its own customer_id — cross-wallet access is
impossible by construction.

Persistence: when a db_path is given, sessions live in a SQLite table so
they survive process restarts (Fly.io auto-stop / scale-to-zero restarts
the container constantly). Without db_path the store is memory-only.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import uuid
from typing import Optional, Tuple


class SessionStore:
    """SQLite-backed session store; falls back to memory without db_path."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self._memory: dict = {}
        self._lock = threading.Lock()
        if db_path:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        customer_id TEXT PRIMARY KEY,
                        token TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def create(self) -> Tuple[str, str]:
        customer_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        import time
        now = time.time()
        with self._lock:
            if self.db_path:
                with self._conn() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO sessions (customer_id, token, created_at) "
                        "VALUES (?, ?, ?)",
                        (customer_id, token, now),
                    )
                    conn.commit()
            else:
                self._memory[customer_id] = token
        return customer_id, token

    def verify(self, customer_id: str, token: str) -> bool:
        if not customer_id or not token:
            return False
        with self._lock:
            if self.db_path:
                with self._conn() as conn:
                    row = conn.execute(
                        "SELECT token FROM sessions WHERE customer_id = ?",
                        (customer_id,),
                    ).fetchone()
                expected = row[0] if row else None
            else:
                expected = self._memory.get(customer_id)
        if expected is None:
            return False
        return secrets.compare_digest(expected, token)
