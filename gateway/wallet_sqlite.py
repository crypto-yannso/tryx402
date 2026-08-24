"""SQLite-backed persistent wallet for tryx402.

Drop-in replacement for gateway.wallet.Wallet with on-disk persistence.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Dict, List, Optional

from .wallet import InsufficientBalance, Transaction

__all__ = ["SQLiteWallet"]


class SQLiteWallet:
    """Thread-safe SQLite-backed wallet."""

    def __init__(self, db_path: str, customer_id: str) -> None:
        self.db_path = db_path
        self.customer_id = customer_id
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    customer_id TEXT NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('credit', 'debit')),
                    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
                    description TEXT NOT NULL,
                    stripe_session_id TEXT,
                    timestamp REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_customer ON transactions(customer_id)"
            )
            conn.commit()

    def get_balance(self) -> int:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT "
                    "COALESCE(SUM(CASE WHEN type='credit' THEN amount_cents ELSE -amount_cents END), 0) "
                    "FROM transactions WHERE customer_id = ?",
                    (self.customer_id,),
                ).fetchone()
                return row[0]

    def credit(
        self,
        amount_cents: int,
        description: str,
        stripe_session_id: Optional[str] = None,
    ) -> Transaction:
        if amount_cents <= 0:
            raise ValueError("amount_cents must be positive")
        txn = Transaction(
            customer_id=self.customer_id,
            type="credit",
            amount_cents=amount_cents,
            description=description,
            stripe_session_id=stripe_session_id,
        )
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO transactions (customer_id, type, amount_cents, description, stripe_session_id, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.customer_id,
                        "credit",
                        amount_cents,
                        description,
                        stripe_session_id,
                        txn.timestamp,
                    ),
                )
                conn.commit()
        return txn

    def debit(self, amount_cents: int, description: str) -> Transaction:
        if amount_cents <= 0:
            raise ValueError("amount_cents must be positive")
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(CASE WHEN type='credit' THEN amount_cents ELSE -amount_cents END), 0) "
                    "FROM transactions WHERE customer_id = ?",
                    (self.customer_id,),
                ).fetchone()
                balance = row[0]
                if balance < amount_cents:
                    raise InsufficientBalance(
                        f"Insufficient balance: {balance} cents, need {amount_cents}"
                    )
                txn = Transaction(
                    customer_id=self.customer_id,
                    type="debit",
                    amount_cents=amount_cents,
                    description=description,
                )
                conn.execute(
                    "INSERT INTO transactions (customer_id, type, amount_cents, description, stripe_session_id, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.customer_id,
                        "debit",
                        amount_cents,
                        description,
                        None,
                        txn.timestamp,
                    ),
                )
                conn.commit()
                return txn

    def get_history(self) -> List[Dict]:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT customer_id, type, amount_cents, description, stripe_session_id, timestamp "
                    "FROM transactions WHERE customer_id = ? ORDER BY timestamp ASC",
                    (self.customer_id,),
                ).fetchall()
                return [
                    {
                        "customer_id": r[0],
                        "type": r[1],
                        "amount_cents": r[2],
                        "description": r[3],
                        "stripe_session_id": r[4],
                        "timestamp": r[5],
                    }
                    for r in rows
                ]
