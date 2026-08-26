"""SQLite-backed email → customer_id index.

Every Stripe checkout.session.completed webhook writes a row here.
Agents use gateway_lookup(email) to recover the wallet after context loss.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Optional


class EmailWalletIndex:
    """Thread-safe mapping from email to the most recent customer_id."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._memory: dict = {}
        if db_path:
            self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wallet_emails (
                    customer_email TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wallet_email ON wallet_emails(customer_email)"
            )
            conn.commit()

    def register_payment(self, customer_email: str, customer_id: str) -> str:
        """Persist email→customer_id mapping. Returns the customer_id."""
        if not customer_email or not customer_id:
            return customer_id
        import time
        now = time.time()
        with self._lock:
            if self.db_path:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO wallet_emails (customer_email, customer_id, created_at) "
                        "VALUES (?, ?, ?)",
                        (customer_email.lower(), customer_id, now),
                    )
                    conn.commit()
            else:
                self._memory[customer_email.lower()] = customer_id
        return customer_id

    def lookup(self, customer_email: str) -> Optional[str]:
        """Return the most recent customer_id for an email, or None."""
        if not customer_email:
            return None
        email = customer_email.lower()
        with self._lock:
            if self.db_path:
                with sqlite3.connect(self.db_path) as conn:
                    row = conn.execute(
                        "SELECT customer_id FROM wallet_emails "
                        "WHERE customer_email = ? ORDER BY created_at DESC LIMIT 1",
                        (email,),
                    ).fetchone()
                return row[0] if row else None
            else:
                return self._memory.get(email)