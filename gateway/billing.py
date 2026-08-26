"""Minimal Stripe client for tryx402 billing.

Uses urllib only — no external dependencies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

__all__ = [
    "StripeBilling",
    "StripeConfigError",
    "StripePaymentError",
    "verify_webhook",
    "create_checkout_session",
]


class StripeConfigError(Exception):
    """Raised when Stripe is not configured."""


class StripePaymentError(Exception):
    """Raised on Stripe API / payment errors."""


def _require_env(var: str, label: str) -> str:
    value = os.environ.get(var, "").strip()
    if not value:
        raise StripeConfigError(f"Missing {label}. Set {var}.")
    return value


def _api_get(url: str, secret: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {secret}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise StripePaymentError(f"Stripe API error {exc.code}: {body}") from exc
    except Exception as exc:
        raise StripePaymentError(f"Stripe request failed: {exc}") from exc


def _api_post(url: str, payload: Dict[str, Any], secret: str) -> Dict[str, Any]:
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Bearer {secret}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise StripePaymentError(f"Stripe API error {exc.code}: {body}") from exc
    except Exception as exc:
        raise StripePaymentError(f"Stripe request failed: {exc}") from exc


class StripeBilling:
    """Minimal Stripe client for tryx402."""

    def __init__(
        self,
        secret_key: Optional[str] = None,
        price_id: Optional[str] = None,
        success_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
        webhook_secret: Optional[str] = None,
        amount_cents: Optional[int] = None,
        currency: Optional[str] = None,
    ) -> None:
        self.secret_key = secret_key or _require_env("TRYX402_STRIPE_SECRET_KEY", "Stripe secret key")
        self.price_id = price_id or os.environ.get("TRYX402_STRIPE_PRICE_ID", "")
        self.success_url = success_url or os.environ.get(
            "TRYX402_STRIPE_SUCCESS_URL", "https://tryx402.fly.dev/billing/success"
        )
        self.cancel_url = cancel_url or os.environ.get(
            "TRYX402_STRIPE_CANCEL_URL", "https://tryx402.fly.dev/billing/cancel"
        )
        self.webhook_secret = webhook_secret or os.environ.get("TRYX402_STRIPE_WEBHOOK_SECRET", "")
        # Per-request amount/currency: passed explicitly by callers instead of
        # mutating global os.environ (which is a cross-request race).
        self.amount_cents = amount_cents
        self.currency = currency

    def create_checkout_session(
        self,
        customer_email: str,
        *,
        mode: str = "subscription",
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Create a Stripe Checkout session.

        Returns the Stripe session dict including the `url` to redirect the user to.
        """
        if mode not in ("subscription", "payment"):
            raise ValueError(f"mode must be 'subscription' or 'payment', got {mode!r}")

        payload: Dict[str, Any] = {
            "mode": mode,
            "customer_email": customer_email,
            "success_url": self.success_url,
            "cancel_url": self.cancel_url,
            "allow_promotion_codes": "true",
        }

        if mode == "subscription":
            if not self.price_id:
                raise StripeConfigError("TRYX402_STRIPE_PRICE_ID is required for subscription mode.")
            payload["line_items"] = [{"price": self.price_id, "quantity": 1}]
        else:
            amount = self.amount_cents or os.environ.get("TRYX402_STRIPE_AMOUNT_CENTS")
            currency = self.currency or os.environ.get("TRYX402_STRIPE_CURRENCY", "usd")
            if not amount:
                raise StripeConfigError("TRYX402_STRIPE_AMOUNT_CENTS is required for one-time mode.")
            # Use bracket-notation keys so urlencode preserves Stripe's nested
            # array shape. urlencode cannot flatten nested dicts/lists by itself.
            base = "line_items[0]"
            payload[f"{base}[price_data][currency]"] = currency
            payload[f"{base}[price_data][unit_amount]"] = int(amount)
            payload[f"{base}[price_data][product_data][name]"] = "tryx402 usage credit"
            payload[f"{base}[quantity]"] = 1

        if metadata:
            for key, value in metadata.items():
                payload[f"metadata[{key}]"] = str(value)
            if "customer_id" in metadata and "client_reference_id" not in payload:
                payload["client_reference_id"] = str(metadata["customer_id"])

        return _api_post("https://api.stripe.com/v1/checkout/sessions", payload, self.secret_key)

    def get_subscription(self, subscription_id: str) -> Dict[str, Any]:
        return _api_get(
            f"https://api.stripe.com/v1/subscriptions/{subscription_id}",
            self.secret_key,
        )

    def retrieve_session(self, session_id: str) -> Dict[str, Any]:
        return _api_get(
            f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
            self.secret_key,
        )

    def is_subscription_active(self, subscription_id: str) -> bool:
        try:
            sub = self.get_subscription(subscription_id)
            return sub.get("status") in ("active", "trialing")
        except Exception:
            return False

    def create_product_and_price(self, product_name: str, price_cents: int, currency: str = "eur") -> Dict[str, Any]:
        """Create a Stripe product and one-time price.

        Returns:
            Dict with product_id, price_id, product_name, price_cents, currency
        """
        if not self.secret_key or self.secret_key == "sk_test_dummy":
            raise StripeConfigError("Stripe secret key not configured")
        if price_cents <= 0:
            raise ValueError("price_cents must be positive")

        # Create product
        product_data = _api_post("https://api.stripe.com/v1/products", {
            "name": product_name,
            "type": "service",
        }, self.secret_key)
        product_id = product_data.get("id", f"prod_dummy_{product_name[:10]}")

        # Create price
        price_data = _api_post("https://api.stripe.com/v1/prices", {
            "product": product_id,
            "unit_amount": str(price_cents),
            "currency": currency.lower(),
        }, self.secret_key)
        price_id = price_data.get("id", f"price_dummy_{price_cents}")

        return {
            "product_id": product_id,
            "price_id": price_id,
            "product_name": product_name,
            "price_cents": price_cents,
            "currency": currency.lower(),
        }


def verify_webhook(
    payload_bytes: bytes,
    sig_header: str,
    webhook_secret: str,
) -> Dict[str, Any]:
    """Verify a Stripe webhook signature and return the parsed event."""
    if not webhook_secret:
        raise StripeConfigError("Missing webhook secret. Set TRYX402_STRIPE_WEBHOOK_SECRET.")

    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(","))
        timestamp = parts["t"]
        signature = parts["v1"]
    except Exception as exc:
        raise StripePaymentError(f"Invalid Stripe signature header: {exc}") from exc

    signed_payload = f"{timestamp}.{payload_bytes.decode('utf-8', errors='replace')}"
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        signed_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise StripePaymentError("Invalid Stripe webhook signature.")

    return json.loads(payload_bytes)


def create_checkout_session(
    customer_email: str,
    *,
    mode: str = "subscription",
    metadata: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Convenience wrapper using env-configured credentials."""
    billing = StripeBilling()
    return billing.create_checkout_session(customer_email, mode=mode, metadata=metadata)
