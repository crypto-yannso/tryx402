"""One-object facade — the easy embed for agents.

    from gateway import Gateway
    gw = Gateway(max_budget_usd=1.0)
    data = gw.call("https://stable-deepline.dev/api/email/validate",
                   body={"email": "x@y.com"}, price=0.03)
    print(gw.spent_usd)          # what this session has spent

Safe by default: hard budget cap, idempotency, cost ledger. No wallet, no crypto
in sight — the local AgentCash CLI settles payment underneath.
"""
from __future__ import annotations

from . import catalog
from .client import SafeClient


class Gateway:
    def __init__(self, max_budget_usd=None, binary=None, idempotent=True, **client_kwargs):
        self.binary = binary
        self._client = SafeClient(binary=binary, max_budget_usd=max_budget_usd,
                                  idempotent=idempotent, **client_kwargs)

    def call(self, url, body=None, *, method="POST", price=None,
             max_amount=None, account=None):
        """Call any AgentCash/x402 endpoint safely. `price` is the expected USD
        cost (used for the budget cap and billing pre-auth)."""
        return self._client.call(url, method=method, body=body,
                                 expected_price=price, max_amount=max_amount, account=account)

    def search(self, query, limit=10):
        """Find endpoints across the AgentCash catalogue by intent."""
        return catalog.search(query, binary=self.binary, limit=limit)

    def discover(self, origin):
        """List one origin's endpoints (with prices)."""
        return catalog.discover(origin, binary=self.binary)

    @property
    def spent_usd(self) -> float:
        return self._client.ledger.total_usd

    def spend_by_origin(self) -> dict:
        return self._client.ledger.by_origin()

    @property
    def ledger(self):
        return self._client.ledger
