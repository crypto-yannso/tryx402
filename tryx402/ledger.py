"""Immutable-ish cost ledger: one event per paid call, so spend is measured and
reconcilable (provider cost is tracked in USD — the rail settles in USDC)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CostEvent:
    endpoint: str
    origin: str
    price_usd: float
    paid: bool
    tx_hash: str | None = None
    account: str | None = None


@dataclass
class Ledger:
    # Provider cost only — the rail settles in USDC (~USD). Customer-currency
    # conversion lives in the billing layer (accounts.py), not here.
    events: list = field(default_factory=list)
    seen_events: set = field(default_factory=set)   # processed webhook event ids

    def record(self, ev: CostEvent) -> None:
        self.events.append(ev)

    def seen(self, event_id: str) -> bool:
        return event_id in self.seen_events

    def mark(self, event_id: str) -> None:
        self.seen_events.add(event_id)

    @property
    def total_usd(self) -> float:
        return round(sum(e.price_usd for e in self.events if e.paid), 6)

    def by_origin(self) -> dict:
        out: dict = {}
        for e in self.events:
            if e.paid:
                out[e.origin] = round(out.get(e.origin, 0.0) + e.price_usd, 6)
        return out

    def by_account(self) -> dict:
        out: dict = {}
        for e in self.events:
            if e.paid and e.account:
                out[e.account] = round(out.get(e.account, 0.0) + e.price_usd, 6)
        return out
