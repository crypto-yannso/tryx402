"""Proxy configuration for tryx402 commission layer.

The proxy adds a configurable commission on top of provider prices.
This is how tryx402 makes money: every call through the hosted service
incurs a small markup that goes to ARTAIFACT SAS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


__all__ = ["ProxyConfig", "DEFAULT_COMMISSION_RATE", "DEFAULT_MIN_COMMISSION_CENTS"]


DEFAULT_COMMISSION_RATE = 0.10  # 10% commission on every call
DEFAULT_MIN_COMMISSION_CENTS = 50  # minimum 50 cents commission


@dataclass
class ProxyConfig:
    """Configuration for the proxy commission layer.

    Args:
        commission_rate: fraction added to every call (0.10 = 10%)
        min_commission_cents: minimum commission in cents (floor)
    """

    commission_rate: float = DEFAULT_COMMISSION_RATE
    min_commission_cents: int = DEFAULT_MIN_COMMISSION_CENTS

    def calculate_total(self, price_cents: int) -> int:
        """Calculate total debit (price + commission) in cents.

        Args:
            price_cents: provider price in cents

        Returns:
            Total amount to debit from wallet (price + commission)
        """
        if price_cents < 0:
            raise ValueError("price_cents must be non-negative")
        commission = max(
            int(price_cents * self.commission_rate),
            self.min_commission_cents,
        )
        return price_cents + commission

    def breakdown(self, price_cents: int) -> Dict[str, int]:
        """Return a detailed breakdown of the cost.

        Returns:
            Dict with price_cents, commission_cents, total_cents
        """
        total = self.calculate_total(price_cents)
        commission = total - price_cents
        return {
            "price_cents": price_cents,
            "commission_cents": commission,
            "total_cents": total,
            "commission_rate": self.commission_rate,
        }
