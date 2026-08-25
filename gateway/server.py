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
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
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
    store: SessionStore = request.app.state.session_store
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
app.state.price_registry = PriceRegistry()


def _wire_session_store(application: FastAPI) -> None:
    """Attach a SQLite-backed SessionStore sharing the wallet DB."""
    db_path = os.environ.get("TRYX402_DB_PATH", _DB_PATH)
    application.state.session_store = SessionStore(db_path)


_TOOLS_DB_SEEDED: set = set()


def _seed_registry_from_tools_db(registry: PriceRegistry, tools_db: str) -> bool:
    """Seed ONE registry object from a tools DB, once per (process, db path).

    The cache is keyed by db path but the seeding always targets the given
    registry — a fresh create_app() gets its own seeded registry even if the
    module-level app was already wired with the same env.
    """
    key = (id(registry), tools_db)
    if tools_db and os.path.exists(tools_db) and key not in _TOOLS_DB_SEEDED:
        try:
            registry.seed_from_tools_db(tools_db)
            _TOOLS_DB_SEEDED.add(key)
            return True
        except Exception:
            pass  # empty registry -> all proxy calls rejected as unknown
    return False


def _wire_price_registry(application: FastAPI) -> None:
    """Attach and seed this application's price registry from the tools DB."""
    try:
        existing = application.state.price_registry
    except AttributeError:
        existing = None
    if existing is None:
        application.state.price_registry = PriceRegistry()
    tools_db = os.environ.get("TRYX402_TOOLS_DB_PATH", "")
    _seed_registry_from_tools_db(application.state.price_registry, tools_db)


_wire_session_store(app)
_wire_price_registry(app)


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
def create_session(request: Request) -> SessionResponse:
    """Mint an anonymous session: customer_id + bearer token bound together."""
    customer_id, token = request.app.state.session_store.create()
    return SessionResponse(customer_id=customer_id, token=token)


@app.get("/v1/wallet/balance", response_model=BalanceResponse)
def get_balance(request: Request) -> BalanceResponse:
    customer_id = _authenticate(request, request.app)
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
    customer_id = _authenticate(request, request.app)
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
    customer_id = _authenticate(request, request.app)
    wallet = _get_wallet(customer_id)

    # Server-side pricing + SSRF allowlist
    registry: PriceRegistry = request.app.state.price_registry
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
# x402-native facade (aggregator/agent discovery + payment)
# ---------------------------------------------------------------------------

from .x402_facade import build_402_response, build_accepts_for_tool, FacadeConfigError


def _facade_pay_to() -> str:
    """Settlement wallet address for x402 payments (env-configured)."""
    return os.environ.get("TRYX402_PAY_TO_ADDRESS", "")


def _facade_resource_url(request: Request, path: str) -> str:
    """Absolute public URL of a facade path (for `resource` in 402 payloads)."""
    base = os.environ.get("TRYX402_PUBLIC_URL", "").rstrip("/")
    if base:
        return f"{base}{path}"
    # Fall back to the request's own host (correct behind Fly.io proxy headers)
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{scheme}://{host}{path}"


class X402ToolsResponse(BaseModel):
    x402Version: int
    tools: list


@app.get("/v1/x402/tools", response_model=X402ToolsResponse)
def x402_tools(request: Request) -> X402ToolsResponse:
    """Public discovery endpoint for agents and aggregators.

    Lists every registered public origin with its server-set price as an
    x402 `accepts` entry. No auth: this is the crawlable catalogue.
    """
    registry: PriceRegistry = request.app.state.price_registry
    pay_to = _facade_pay_to()
    network = os.environ.get("TRYX402_NETWORK", "base")
    asset = os.environ.get("TRYX402_ASSET", "")
    token_name = os.environ.get("TRYX402_TOKEN_NAME", "USD Coin")
    token_version = os.environ.get("TRYX402_TOKEN_VERSION", "2")
    extra = {"name": token_name, "version": token_version}
    tools = []
    for origin, price_cents, allow_private in registry.items():
        if allow_private:
            continue  # explicitly-private origins never listed
        try:
            accepts = build_accepts_for_tool(
                resource_url=_facade_resource_url(request, "/v1/x402/call"),
                origin=origin,
                price_cents=price_cents,
                pay_to=pay_to,
                network=network,
                asset=asset or None,
                extra=extra,
            )
        except FacadeConfigError:
            continue  # no settlement address configured -> nothing listable
        tools.append({
            "origin": origin,
            "resource": _facade_resource_url(request, "/v1/x402/call"),
            "scheme": "exact",
            "network": accepts[0]["network"],
            "asset": accepts[0]["asset"],
            "maxAmountRequired": accepts[0]["maxAmountRequired"],
            "priceDisplay": f"${price_cents / 100:.2f}",
            "payTo": accepts[0]["payTo"],
        })
    return X402ToolsResponse(x402Version=1, tools=tools)


@app.get("/v1/x402/listing")
def x402_listing(request: Request):
    """Syndication: registry as a generic x402 resources document.

    Public, no-auth. Aggregators and discovery crawlers consume this to
    index every tryx402-wrapped endpoint. Private origins are excluded;
    exported prices are exactly what /v1/x402/call will charge.
    """
    from .syndication import export_listing, SyndicationConfigError

    registry: PriceRegistry = request.app.state.price_registry
    try:
        doc = export_listing(
            registry,
            base_url=_facade_resource_url(request, ""),
            pay_to=_facade_pay_to(),
        )
    except SyndicationConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(doc)


@app.get("/v1/x402/bazaar.json")
def x402_bazaar_feed(request: Request):
    """Syndication: Bazaar-style flat feed for aggregator indexing."""
    from .syndication import bazaar_feed, SyndicationConfigError

    registry: PriceRegistry = request.app.state.price_registry
    try:
        feed = bazaar_feed(
            registry,
            base_url=_facade_resource_url(request, ""),
            pay_to=_facade_pay_to(),
        )
    except SyndicationConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(feed)


def _uses_private_check(origin: str) -> bool:
    from gateway.registry import _is_private_origin
    return _is_private_origin(origin)


SITE_DIR = os.path.join(os.path.dirname(__file__), "site")


def _site_page(request: Request, base: str):
    """Serve a static site page with language negotiation (fr variant first)."""
    from fastapi.responses import FileResponse
    from fastapi import HTTPException

    lang = (request.headers.get("accept-language") or "").lower()
    fr_path = os.path.join(SITE_DIR, f"{base}.fr.html")
    if lang.startswith("fr") and os.path.exists(fr_path):
        return FileResponse(fr_path)
    en_path = os.path.join(SITE_DIR, f"{base}.html")
    if os.path.exists(en_path):
        return FileResponse(en_path)
    raise HTTPException(status_code=404, detail="Not Found")


@app.get("/tools")
@app.get("/tools/")
def tools_page(request: Request):
    """Visual web catalogue (linked from www.tryx402.app nav)."""
    return _site_page(request, "tools")


@app.get("/provider")
@app.get("/provider/")
def provider_page(request: Request):
    """Provider portal page (linked from www.tryx402.app nav)."""
    return _site_page(request, "provider")


class X402CallRequest(BaseModel):
    origin: str
    path: str = "/"
    method: str = "POST"
    body: Optional[Dict] = None


@app.post("/v1/x402/call")
def x402_call(request: Request, req: X402CallRequest):
    """Pay-per-call facade for external x402 agents.

    No X-PAYMENT -> 402 with payment requirements (spec-conformant).
    Valid X-PAYMENT -> verify via facilitator, then proxy to the origin.
    """
    registry: PriceRegistry = request.app.state.price_registry
    url = f"{req.origin.rstrip('/')}{req.path}"
    try:
        price_cents = registry.lookup(url)
    except UnknownOriginError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PrivateOriginError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    pay_to = _facade_pay_to()
    if not pay_to:
        raise HTTPException(
            status_code=503,
            detail="x402 settlement address not configured (TRYX402_PAY_TO_ADDRESS)",
        )

    payment_header = request.headers.get("X-PAYMENT", "")
    if not payment_header:
        network = os.environ.get("TRYX402_NETWORK", "base")
        asset = os.environ.get("TRYX402_ASSET", "")
        token_name = os.environ.get("TRYX402_TOKEN_NAME", "USD Coin")
        token_version = os.environ.get("TRYX402_TOKEN_VERSION", "2")
        extra = {"name": token_name, "version": token_version}
        return JSONResponse(
            status_code=402,
            content=build_402_response(
                resource_url=_facade_resource_url(request, "/v1/x402/call"),
                description=f"tryx402 proxy for {req.origin}",
                price_cents=price_cents,
                pay_to=pay_to,
                network=network,
                asset=asset or None,
                extra=extra,
            ),
        )

    # Payment present -> delegated to the payment-flow cycle (next).
    from .x402_payments import handle_paid_call
    return handle_paid_call(
        request=request, req=req, price_cents=price_cents,
        pay_to=pay_to, resource_url=_facade_resource_url(request, "/v1/x402/call"),
    )


# ---------------------------------------------------------------------------
# ASGI factory
# ---------------------------------------------------------------------------

@app.get("/api/v1/tools")
@app.get("/api/v1/tools/")
def tools_list(active_only: bool = True):
    """Public catalogue API backing the /tools web page.

    Reads the provider tools DB (same file the PriceRegistry seeds from)
    and seeds verified tools on first access so the catalogue is never
    empty on a fresh volume.
    """
    from .tools_db import init_db, list_tools, seed_verified_tools

    db_path = os.environ.get("TRYX402_TOOLS_DB_PATH", "")
    if not db_path:
        return {"success": True, "count": 0, "tools": []}
    init_db(db_path)
    seed_verified_tools(db_path)
    tools = list_tools(db_path, active_only)
    return {"success": True, "count": len(tools), "tools": tools}


def _setup_custom_openapi(application: FastAPI) -> None:
    def custom_openapi():
        if application.openapi_schema:
            return application.openapi_schema
        openapi_schema = get_openapi(
            title="tryx402 — Universal Agent Payments & Gateway",
            version="0.4.0",
            description="Autonomous pay-per-call proxy and microtransaction payment gateway over x402.",
            routes=application.routes,
        )
        openapi_schema["info"]["x-guidance"] = (
            "tryx402 enables AI agents to discover, invoke, and pay for 76+ microservices and web2 tools. "
            "Call POST /v1/x402/call with origin and path. On 402, sign the exact amount and retry with X-PAYMENT."
        )
        openapi_schema["info"]["contact"] = {
            "name": "tryx402 Team",
            "email": "yann@artaifact.com",
            "url": "https://www.tryx402.app",
        }

        # Enhance payable route metadata for discovery bots (x402scan, AgentCash, Bazaar)
        paths = openapi_schema.get("paths", {})
        if "/v1/x402/call" in paths and "post" in paths["/v1/x402/call"]:
            post_op = paths["/v1/x402/call"]["post"]
            post_op.setdefault("responses", {})
            post_op["responses"]["402"] = {
                "description": "Payment Required - returns x402 payment challenge with exact scheme requirements"
            }
            post_op["x-payment-info"] = {
                "price": {
                    "mode": "dynamic",
                    "currency": "USD",
                    "min": "0.01",
                    "max": "1.00",
                },
                "protocols": [{"x402": {}}],
            }

        application.openapi_schema = openapi_schema
        return application.openapi_schema

    application.openapi = custom_openapi


_setup_custom_openapi(app)


def create_app() -> FastAPI:
    new_app = FastAPI(
        title="tryx402 hosted service",
        description="Wallet, billing, and telemetry for tryx402.",
        version="0.4.0",
    )
    # Persistent session store sharing the wallet DB — sessions survive
    # restarts (Fly.io scale-to-zero).
    _wire_session_store(new_app)
    _wire_price_registry(new_app)
    for route in app.routes:
        if hasattr(route, "endpoint"):
            new_app.routes.append(route)
    _setup_custom_openapi(new_app)
    return new_app


# ---------------------------------------------------------------------------
# CLI entry point (standalone test server)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("TRYX402_HOST", "0.0.0.0")
    port = int(os.environ.get("TRYX402_PORT", "8080"))
    print(f"tryx402 server listening on http://{host}:{port}")
    uvicorn.run("gateway.server:app", host=host, port=port, reload=False)
