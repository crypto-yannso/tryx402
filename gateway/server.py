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
from pydantic import BaseModel

from .billing import StripeBilling, StripeConfigError, StripePaymentError, verify_webhook
from .wallet_sqlite import SQLiteWallet, InsufficientBalance

__all__ = ["create_app"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DB_PATH = os.environ.get("TRYX402_DB_PATH", "/tmp/tryx402_wallets.db")
_API_KEY = os.environ.get("TRYX402_API_KEY", "dev-key")  # demo mode
_WEBHOOK_SECRET = os.environ.get("TRYX402_STRIPE_WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_customer_id(request: Request) -> str:
    """Extract customer_id from X-API-Key header (demo: key == customer_id)."""
    api_key = request.headers.get("X-API-Key", "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    # Demo mode: use the API key itself as customer_id
    return api_key


def _get_wallet(customer_id: str) -> SQLiteWallet:
    return SQLiteWallet(_DB_PATH, customer_id)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="tryx402 hosted service",
    description="Wallet, billing, and telemetry for tryx402.",
    version="0.3.1",
)


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


@app.get("/v1/wallet/balance", response_model=BalanceResponse)
def get_balance(request: Request) -> BalanceResponse:
    customer_id = _get_customer_id(request)
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
    customer_id = _get_customer_id(request)
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


@app.post("/v1/billing/checkout")
def create_checkout(req: CheckoutRequest) -> Dict[str, str]:
    try:
        billing = StripeBilling()
        os.environ["TRYX402_STRIPE_AMOUNT_CENTS"] = str(req.amount_cents)
        os.environ["TRYX402_STRIPE_CURRENCY"] = req.currency
        session = billing.create_checkout_session(
            req.customer_email,
            mode="payment",
        )
        return {"url": session["url"], "session_id": session["id"]}
    except StripeConfigError as exc:
        raise HTTPException(status_code=501, detail=str(exc))
    except StripePaymentError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/v1/billing/webhook")
async def stripe_webhook(request: Request) -> Dict[str, object]:
    if not _WEBHOOK_SECRET:
        raise HTTPException(status_code=501, detail="Webhook secret not configured")
    body_bytes = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = verify_webhook(body_bytes, sig_header, _WEBHOOK_SECRET)
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
    # For now, just acknowledge
    return {"ok": "True"}


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
