"""Minimal FastAPI server for tryx402 hosted service.

Endpoints:
    GET  /health
    GET  /v1/wallet/balance      (auth via X-API-Key header)
    GET  /v1/wallet/transactions (auth via X-API-Key header)
    POST /v1/billing/checkout
    POST /v1/billing/webhook
    POST /v1/telemetry
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .billing import StripeBilling, StripeConfigError, StripePaymentError, verify_webhook
from .wallet_sqlite import SQLiteWallet, InsufficientBalance
from .proxy import ProxyConfig, DEFAULT_COMMISSION_RATE, DEFAULT_MIN_COMMISSION_CENTS
from .registry import PriceRegistry, UnknownOriginError, PrivateOriginError
from .sessions import SessionStore

__all__ = ["create_app"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DB_PATH = os.environ.get("TRYX402_DB_PATH", "/tmp/tryx402_wallets.db")
_WEBHOOK_SECRET = os.environ.get("TRYX402_STRIPE_WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_wallet(customer_id: str) -> SQLiteWallet:
    # Re-read env in case it changed since import
    db_path = os.environ.get("TRYX402_DB_PATH", _DB_PATH)
    return SQLiteWallet(db_path, customer_id)


def _authenticate(request: Request, app: "FastAPI") -> str:
    """Require X-Customer-ID + X-Session-Token bound together.

    Returns the authenticated customer_id or raises 401. A token is only
    valid for its own customer_id — no cross-wallet access.
    """
    customer_id = request.headers.get("X-Customer-ID", "").strip()
    token = request.headers.get("X-Session-Token", "").strip()
    store: SessionStore = app.state.session_store
    if not customer_id or not token or not store.verify(customer_id, token):
        raise HTTPException(status_code=401, detail="Invalid or missing session credentials")
    return customer_id


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="tryx402 hosted service",
    description="Wallet, billing, and telemetry for tryx402.",
    version="0.4.0",
)
app.state.session_store = SessionStore()
app.state.price_registry = PriceRegistry()


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Wallet endpoints
# ---------------------------------------------------------------------------

class BalanceResponse(BaseModel):
    customer_id: str
    balance_cents: int
    balance_display: str


class SessionResponse(BaseModel):
    customer_id: str
    token: str


@app.post("/v1/auth/session", response_model=SessionResponse)
def create_session() -> SessionResponse:
    """Mint an anonymous session: customer_id + bearer token bound together."""
    customer_id, token = app.state.session_store.create()
    return SessionResponse(customer_id=customer_id, token=token)


@app.get("/v1/wallet/balance", response_model=BalanceResponse)
def get_balance(request: Request) -> BalanceResponse:
    customer_id = _authenticate(request, app)
    wallet = _get_wallet(customer_id)
    balance = wallet.get_balance()
    return BalanceResponse(
        customer_id=customer_id,
        balance_cents=balance,
        balance_display=f"{balance / 100:.2f} EUR",
    )


class TransactionResponse(BaseModel):
    customer_id: str
    type: str
    amount_cents: int
    description: str
    stripe_session_id: Optional[str]
    timestamp: float


@app.get("/v1/wallet/transactions")
def get_transactions(request: Request) -> Dict[str, object]:
    customer_id = _authenticate(request, app)
    wallet = _get_wallet(customer_id)
    history = wallet.get_history()
    return {"customer_id": customer_id, "transactions": history}


# ---------------------------------------------------------------------------
# Billing endpoints
# ---------------------------------------------------------------------------

class CheckoutRequest(BaseModel):
    customer_email: str
    amount_cents: int
    currency: str = "eur"


class BillingSetupRequest(BaseModel):
    product_name: str
    price_cents: int
    currency: str = "eur"
    
    @field_validator('price_cents')
    @classmethod
    def validate_price_cents(cls, v):
        if v <= 0:
            raise ValueError('price_cents must be positive')
        return v


@app.post("/v1/billing/checkout")
def create_checkout(req: CheckoutRequest) -> Dict[str, str]:
    try:
        billing = StripeBilling(amount_cents=req.amount_cents, currency=req.currency)
        session = billing.create_checkout_session(
            req.customer_email,
            mode="payment",
        )
        return {"url": session["url"], "session_id": session["id"]}
    except StripeConfigError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except StripePaymentError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/v1/billing/setup")
def billing_setup(req: BillingSetupRequest) -> Dict[str, object]:
    """Create a Stripe product + one-time price for wallet top-ups.

    This endpoint is called once by the developer/ops to provision
    the Stripe product. It returns product_id and price_id, which
    should then be stored in env vars:
      TRYX402_STRIPE_PRICE_ID=price_xxx
    """
    try:
        billing = StripeBilling()
        result = billing.create_product_and_price(
            product_name=req.product_name,
            price_cents=req.price_cents,
            currency=req.currency,
        )
        return result
    except StripeConfigError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except StripePaymentError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/v1/billing/webhook")
async def stripe_webhook(request: Request) -> Dict[str, object]:
    secret = os.environ.get("TRYX402_STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=501, detail="Webhook secret not configured")
    body_bytes = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = verify_webhook(body_bytes, sig_header, secret)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        customer_id = data.get("client_reference_id") or data.get("customer", "")
        if customer_id:
            wallet = _get_wallet(customer_id)
            amount = data.get("amount_total", 0)
            session_id = data.get("id", "")
            # Idempotency: a Stripe session id is credited AT MOST once.
            if session_id and wallet.has_stripe_session(session_id):
                return {"received": True, "type": event_type, "duplicate": True}
            wallet.credit(
                amount_cents=amount,
                description="Stripe recharge",
                stripe_session_id=session_id,
            )

    return {"received": True, "type": event_type}


# ---------------------------------------------------------------------------
# Telemetry endpoint
# ---------------------------------------------------------------------------

class TelemetryRequest(BaseModel):
    install_id: str
    version: str
    python: str
    platform: str


@app.post("/v1/telemetry")
def receive_telemetry(payload: TelemetryRequest) -> Dict[str, object]:
    """Accept anonymous telemetry pings. In production, store in DB."""
    return {"ok": "True"}


# ---------------------------------------------------------------------------
# Proxy endpoint — transparent commission layer
# ---------------------------------------------------------------------------

class ProxyRequest(BaseModel):
    url: str
    body: Optional[Dict] = None
    method: str = "POST"


class ProxyResponse(BaseModel):
    status_code: int
    headers: Dict[str, str]
    body: Optional[str] = None
    cost_cents: int
    commission_cents: int
    total_cents: int
    new_balance_cents: int


@app.post("/v1/proxy/call", response_model=ProxyResponse)
def proxy_call(request: Request, req: ProxyRequest) -> ProxyResponse:
    """Transparent proxy: forward call, debit wallet with commission.

    Security model:
      - price comes from the SERVER registry, never from the client
      - only registered public origins are reachable (anti-SSRF)
      - debit is atomic (no negative balance under concurrency)
      - wallet is refunded if the provider call fails
    """
    customer_id = _authenticate(request, app)
    wallet = _get_wallet(customer_id)

    # Server-side pricing + SSRF allowlist
    registry: PriceRegistry = app.state.price_registry
    try:
        price_cents = registry.lookup(req.url)
    except UnknownOriginError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PrivateOriginError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    config = ProxyConfig(commission_rate=DEFAULT_COMMISSION_RATE,
                         min_commission_cents=DEFAULT_MIN_COMMISSION_CENTS)
    total_cents = config.calculate_total(price_cents)
    breakdown = config.breakdown(price_cents)

    # ATOMIC check+debit — cannot go negative under concurrency
    try:
        wallet.debit_if_affordable(
            amount_cents=total_cents,
            description=f"Proxy call ({req.method} {req.url}) + {DEFAULT_COMMISSION_RATE*100:.0f}% commission",
        )
    except InsufficientBalance:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_balance",
                "required_cents": total_cents,
                "available_cents": wallet.get_balance(),
                "price_cents": price_cents,
                "commission_cents": breakdown["commission_cents"],
            },
        )

    # Forward request to provider
    try:
        import urllib.request
        import urllib.error
        import json as _json
        data = _json.dumps(req.body or {}).encode() if req.method.upper() != "GET" else None
        fwd_req = urllib.request.Request(
            req.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method=req.method.upper(),
        )
        with urllib.request.urlopen(fwd_req, timeout=30) as resp:
            status_code = resp.status
            headers = dict(resp.headers)
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        # Provider failed -> refund the wallet fully (finding 6)
        wallet.refund(
            amount_cents=total_cents,
            description=f"Provider failure ({req.url})",
        )
        raise HTTPException(
            status_code=502,
            detail=f"Provider call failed, wallet refunded ({total_cents} cents)",
        )

    new_balance = wallet.get_balance()
    return ProxyResponse(
        status_code=status_code,
        headers=headers,
        body=body,
        cost_cents=price_cents,
        commission_cents=breakdown["commission_cents"],
        total_cents=total_cents,
        new_balance_cents=new_balance,
    )


# ---------------------------------------------------------------------------
# ASGI factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    return app


# ---------------------------------------------------------------------------
# CLI entry point (standalone test server)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("TRYX402_HOST", "0.0.0.0")
    port = int(os.environ.get("TRYX402_PORT", "8080"))
    print(f"tryx402 server listening on http://{host}:{port}")
    uvicorn.run("gateway.server:app", host=host, port=port, reload=False)
