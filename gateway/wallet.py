"""In-memory wallet for tryx402 customer balances.

Replacement with SQLite/Postgres is a drop-in change later; tests already
exercise only the public interface.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

__all__ = ["Wallet", "InsufficientBalance"]


class InsufficientBalance(Exception):
    """Raised when a debit would exceed the current balance."""


@dataclass
class Transaction:
    customer_id: str
    type: str  # "credit" | "debit"
    amount_cents: int
    description: str
    stripe_session_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


class Wallet:
    """Thread-safe per-customer wallet backed by in-memory storage."""

    def __init__(self, customer_id: str) -> None:
        self.customer_id = customer_id
        self._lock = threading.Lock()
        self._transactions: List[Transaction] = []

    def get_balance(self) -> int:
        with self._lock:
            return sum(
                t.amount_cents if t.type == "credit" else -t.amount_cents
                for t in self._transactions
            )

    def credit(
        self,
        amount_cents: int,
        description: str,
        stripe_session_id: Optional[str] = None,
    ) -> Transaction:
        """Add funds to the wallet."""
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
            self._transactions.append(txn)
        return txn

    def debit(self, amount_cents: int, description: str) -> Transaction:
        """Remove funds from the wallet. Raises InsufficientBalance if not enough."""
        if amount_cents <= 0:
            raise ValueError("amount_cents must be positive")
        with self._lock:
            balance = sum(
                t.amount_cents if t.type == "credit" else -t.amount_cents
                for t in self._transactions
            )
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
            self._transactions.append(txn)
            return txn

    def get_history(self) -> List[Dict]:
        with self._lock:
            return [
                {
                    "customer_id": t.customer_id,
                    "type": t.type,
                    "amount_cents": t.amount_cents,
                    "description": t.description,
                    "stripe_session_id": t.stripe_session_id,
                    "timestamp": t.timestamp,
                }
                for t in self._transactions
            ]
