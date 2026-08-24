"""One-object facade — the easy embed for agents.

    from gateway import Gateway
    gw = Gateway(max_budget_usd=1.0)
    data = gw.call("https://stable-deepline.dev/api/email/validate",
                   body={"email": "x@y.com"}, price=0.03)
    print(gw.spent_usd)          # what this session has spent

Safe by default: hard budget cap, idempotency, cost ledger. No wallet, no crypto
in sight — the local AgentCash CLI settles payment underneath.

Pricing note (0.2.0+): margin and FX live on the hosted service, never in this
SDK. Use quote() to ask the server what a call will cost your account.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import catalog
from .client import SafeClient

DEFAULT_API = os.environ.get("TRYX402_API", "https://tryx402.fly.dev")


class Gateway:
    def __init__(self, max_budget_usd=None, binary=None, idempotent=True,
                 api_base: str | None = None, api_key: str | None = None,
                 **client_kwargs):
        self.binary = binary
        self.api_base = (api_base or DEFAULT_API).rstrip("/")
        self.api_key = api_key or os.environ.get("TRYX402_API_KEY")
        self._client = SafeClient(binary=binary, max_budget_usd=max_budget_usd,
                                  idempotent=idempotent, **client_kwargs)

    def call(self, url, body=None, *, method="POST", price=None,
             max_amount=None, account=None):
        """Call any AgentCash/x402 endpoint safely. `price` is the expected USD
        cost (used for the budget cap)."""
        return self._client.call(url, method=method, body=body,
                                 expected_price=price, max_amount=max_amount, account=account)

    def search(self, query, limit=10):
        """Find endpoints across the AgentCash catalogue by intent."""
        return catalog.search(query, binary=self.binary, limit=limit)

    def discover(self, origin):
        """List one origin's endpoints (with prices)."""
        return catalog.discover(origin, binary=self.binary)

    def quote(self, data_cost_usd: float) -> dict:
        """Ask the hosted service what a call costs YOUR account.

        The server applies its own FX + margin and returns the charge in the
        account's currency (integer minor units + formatted string). Requires
        an API key (pass api_key= or set TRYX402_API_KEY).
        """
        if not self.api_key:
            raise RuntimeError("quote() needs an API key: pass api_key= or set TRYX402_API_KEY")
        url = f"{self.api_base}/v1/quote?data_cost_usd={float(data_cost_usd)}"
        req = urllib.request.Request(url, headers={"X-API-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"quote failed: HTTP {e.code} {e.read().decode()[:200]}") from e

    def check_balance(self) -> dict | None:
        """Check wallet balance on the hosted service.

        Returns the balance dict if an API key is configured, None otherwise.
        """
        if not self.api_key:
            return None
        url = f"{self.api_base}/v1/wallet/balance"
        req = urllib.request.Request(url, headers={"X-API-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception:
            return None

    def recharge(self, amount_cents: int, currency: str = "eur") -> dict:
        """Create a Stripe Checkout session to top up the wallet.

        Requires an API key and TRYX402_STRIPE_SECRET_KEY on the server.
        """
        if not self.api_key:
            raise RuntimeError("recharge() needs an API key: pass api_key= or set TRYX402_API_KEY")
        url = f"{self.api_base}/v1/billing/checkout"
        payload = json.dumps({
            "customer_email": "",  # optional, server can extract from API key
            "amount_cents": amount_cents,
            "currency": currency,
        }).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"X-API-Key": self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"recharge failed: HTTP {e.code} {e.read().decode()[:200]}") from e

    @property
    def spent_usd(self) -> float:
        return self._client.ledger.total_usd

    def spend_by_origin(self) -> dict:
        return self._client.ledger.by_origin()

    @property
    def ledger(self):
        return self._client.ledger
