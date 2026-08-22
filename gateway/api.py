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
from .accounts import AccountStore, FxRates
from .client import Billing, SafeClient


class Gateway:
    def __init__(self, max_budget_usd=None, binary=None, idempotent=True,
                 store_path=None, rates: FxRates | None = None, **client_kwargs):
        self.binary = binary
        self.rates = rates or FxRates()
        self.store = AccountStore.load(store_path) if store_path else None
        billing = (Billing(self.store, self.rates)
                   if self.store is not None else None)
        self._client = SafeClient(binary=binary, max_budget_usd=max_budget_usd,
                                  idempotent=idempotent, billing=billing,
                                  **client_kwargs)

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

    def create_account(self, account_id, currency="USD", margin=0.30):
        if self.store is None:
            raise RuntimeError("no store: pass store_path to Gateway()")
        acct = self.store.create(account_id, currency=currency, margin=margin)
        self.store.save()
        return acct

    def balance(self, account_id) -> int:
        assert self.store is not None
        return self.store.accounts[account_id].balance_minor

    @property
    def ledger(self):
        return self._client.ledger
