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
        self._session: tuple | None = None  # (customer_id, token)

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

    def check_balance(self, api_key: str | None = None) -> dict | None:
        """Check wallet balance on the hosted service.

        Session auth by default; falls back to api_key when provided
        (backward compat for server-side deployments).
        """
        if api_key or self.api_key:
            key = api_key or self.api_key
            url = f"{self.api_base}/v1/wallet/balance"
            req = urllib.request.Request(url, headers={"X-API-Key": key})
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    return json.loads(r.read().decode())
            except Exception:
                return None

        try:
            customer_id, token = self._ensure_session()
        except Exception:
            return None
        url = f"{self.api_base}/v1/wallet/balance"
        req = urllib.request.Request(url, headers={
            "X-Customer-ID": customer_id,
            "X-Session-Token": token,
        })
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
            "customer_email": "",
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

    def _ensure_session(self) -> tuple:
        """Return (customer_id, token), minting the session on first use.

        The customer_id is persisted locally (anon_auth); the token is
        obtained from POST /v1/auth/session and cached for this Gateway's
        lifetime.
        """
        if self._session is None:
            from .anon_auth import get_or_create_customer_id
            customer_id = get_or_create_customer_id()
            req = urllib.request.Request(
                f"{self.api_base}/v1/auth/session",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            # Server-issued customer_id wins (it owns the server-side wallet)
            self._session = (data.get("customer_id", customer_id),
                             data["token"])
        return self._session

    def proxy_call(self, url: str, body=None, *, method: str = "POST") -> dict:
        """Call through the hosted proxy (transparent commission layer).

        This is the revenue-generating path: every call incurs a commission
        (default 10%) that is debited from the wallet.

        Auth: session token minted from /v1/auth/session, bound to the
        persistent anonymous customer ID. Price is set SERVER-side; there is
        deliberately no price_usd parameter anymore.

        Returns the proxy response including breakdown:
          - cost_cents, commission_cents, total_cents, new_balance_cents
        """
        customer_id, token = self._ensure_session()

        proxy_url = f"{self.api_base}/v1/proxy/call"
        payload = json.dumps({
            "url": url,
            "body": body or {},
            "method": method,
        }).encode()
        req = urllib.request.Request(
            proxy_url,
            data=payload,
            headers={
                "X-Customer-ID": customer_id,
                "X-Session-Token": token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"proxy_call failed: HTTP {e.code} {body_text[:200]}") from e

    @property
    def spent_usd(self) -> float:
        return self._client.ledger.total_usd

    def spend_by_origin(self) -> dict:
        return self._client.ledger.by_origin()

    # --- competitive-parity additions (0.3.0) -------------------------------

    def plan(self, steps):
        """Price a multi-call workflow BEFORE spending anything.

        Steps: list of (url, body?, expected_price?) tuples, PlanStep, or
        dicts {url, body, price}. Pure estimation — no wallet activity.
        """
        from tryx402.planner import estimate_plan
        est = estimate_plan(steps, spent_usd=self.spent_usd,
                            max_budget_usd=self._client.max_budget_usd)
        return est.to_dict()

    def receipt(self, endpoint: str, origin: str, price_usd: float,
                tx_hash: str | None = None, **kw) -> dict:
        """Sign a verifiable receipt for the last (or any) paid call."""
        from tryx402.receipts import ReceiptBuilder
        if not hasattr(self, "_receipts"):
            self._receipts = ReceiptBuilder()
        return self._receipts.build(endpoint=endpoint, origin=origin,
                                    price_usd=price_usd, tx_hash=tx_hash, **kw)

    def session(self, cap_usd: float | None = None, ttl_s: int = 3600):
        """Mint a governed sub-session with its own spend cap + breaker.

            sess = gw.session(cap_usd=0.50)
            sess.call(url, expected_price=0.03)

        Blast radius per task; a runaway loop can only burn the session cap.
        """
        from tryx402.sessions import CircuitBreaker, SessionManager, SessionedClient
        if not hasattr(self, "_session_mgr"):
            self._session_mgr = SessionManager()
        tok = self._session_mgr.mint(cap_usd=cap_usd or 1.0, ttl_s=ttl_s)
        return SessionedClient(self._client, tok, self._session_mgr)

    @property
    def ledger(self):
        return self._client.ledger
