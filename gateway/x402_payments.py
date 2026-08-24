"""Paid-call handling for the x402 facade (X-PAYMENT present).

Separated from server.py so the payment flow can be unit-tested in
isolation. The verify step talks to an x402 facilitator; settlement and
provider forwarding happen only after a verified payment.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request
import urllib.error

__all__ = ["handle_paid_call", "FacilitatorError", "decode_x402_header"]


class FacilitatorError(Exception):
    """The facilitator rejected or could not verify the payment."""


def decode_x402_header(header_value: str) -> dict:
    """Decode the base64 JSON X-PAYMENT header into a dict."""
    try:
        return json.loads(base64.b64decode(header_value))
    except Exception as exc:
        raise FacilitatorError(f"malformed X-PAYMENT header: {exc}")


def _facilitator_base() -> str:
    return os.environ.get(
        "TRYX402_FACILITATOR_URL",
        "https://x402.org/facilitator",  # CDP's public facilitator default
    ).rstrip("/")


def verify_with_facilitator(payment_payload: dict,
                            requirements: dict) -> dict:
    """POST /verify to the facilitator. Returns its JSON verdict.

    Raises FacilitatorError on any non-200 or invalid=false verdict.
    Never retries: a verification attempt does not move funds, but the
    follow-up settle does — same no-retry discipline as paid calls.
    """
    body = json.dumps({
        "x402Version": 1,
        "paymentHeader": payment_payload,
        "paymentRequirements": requirements,
    }).encode()
    req = urllib.request.Request(
        f"{_facilitator_base()}/verify",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            verdict = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise FacilitatorError(f"facilitator HTTP {exc.code}")
    except Exception as exc:
        raise FacilitatorError(f"facilitator unreachable: {exc}")
    if not verdict.get("isValid"):
        reason = verdict.get("invalidReason") or "unknown"
        raise FacilitatorError(f"payment invalid: {reason}")
    return verdict


def handle_paid_call(request, req, price_cents: int, pay_to: str,
                     resource_url: str):
    """Full paid path: verify payment -> proxy to origin -> settle.

    Wire format mirrors what server.py's own /v1/proxy/call returns so the
    two faces of the gateway stay symmetric.
    """
    from fastapi.responses import JSONResponse
    from fastapi import HTTPException
    from .proxy import ProxyConfig, DEFAULT_COMMISSION_RATE, DEFAULT_MIN_COMMISSION_CENTS
    from . import server as srv

    # 1) Decode + verify the payment with the facilitator
    header = request.headers.get("X-PAYMENT", "")
    try:
        payment = decode_x402_header(header)
    except FacilitatorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    config = ProxyConfig(commission_rate=DEFAULT_COMMISSION_RATE,
                         min_commission_cents=DEFAULT_MIN_COMMISSION_CENTS)
    total_cents = config.calculate_total(price_cents)

    requirements = {
        "scheme": "exact",
        "network": os.environ.get("TRYX402_NETWORK", "base"),
        "maxAmountRequired": price_cents * 10_000,
        "asset": os.environ.get(
            "TRYX402_ASSET", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
        "payTo": pay_to,
        "resource": resource_url,
    }
    try:
        verdict = verify_with_facilitator(payment, requirements)
    except FacilitatorError as exc:
        raise HTTPException(status_code=402, detail=f"payment rejected: {exc}")

    # 2) Forward to the provider through the SAME guarded transport rules
    url = f"{req.origin.rstrip('/')}{req.path}"
    data = json.dumps(req.body or {}).encode() if req.method.upper() != "GET" else None
    fwd_req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method=req.method.upper(),
    )
    try:
        with urllib.request.urlopen(fwd_req, timeout=30) as resp:
            status_code = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        # Provider failed AFTER payment accepted: record for reconciliation,
        # never auto-retry (a started run bills).
        raise HTTPException(status_code=502, detail={
            "error": "upstream_failed_after_payment",
            "detail": str(exc),
            "payer": (payment.get("x402Version") and None)
                     or payment.get("from", ""),
            "reconciliation_required": True,
        })

    # 3) Settle via facilitator (best-effort record; settle failure is
    #    flagged but does not undo the delivered response)
    settlement = {"settled": False}
    try:
        s_body = json.dumps({
            "x402Version": 1, "paymentHeader": payment,
            "paymentRequirements": requirements,
        }).encode()
        s_req = urllib.request.Request(f"{_facilitator_base()}/settle", data=s_body,
                                       headers={"Content-Type": "application/json"},
                                       method="POST")
        with urllib.request.urlopen(s_req, timeout=15) as s_resp:
            settlement = json.loads(s_resp.read())
    except Exception as exc:
        settlement = {"settled": False, "error": str(exc)}

    return JSONResponse(status_code=200, content={
        "status_code": status_code,
        "body": body,
        "cost_cents": price_cents,
        "commission_cents": config.breakdown(price_cents)["commission_cents"],
        "total_atomic_units": requirements["maxAmountRequired"],
        "settlement": settlement,
    })
