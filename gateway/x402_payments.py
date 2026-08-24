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

from .x402_facade import canonical_network

__all__ = ["handle_paid_call", "FacilitatorError", "decode_x402_header"]


class FacilitatorError(Exception):
    """The facilitator rejected or could not verify the payment."""


def decode_x402_header(header_value: str) -> dict:
    """Decode the base64 JSON X-PAYMENT header into a dict."""
    try:
        return json.loads(base64.b64decode(header_value))
    except Exception as exc:
        raise FacilitatorError(f"malformed X-PAYMENT header: {exc}")


# ---------------------------------------------------------------------------
# Replay protection (in-process; production should back this with Redis/DB)
# ---------------------------------------------------------------------------

_seen_payments: set = set()
_SEEN_PAYMENTS_MAX = 100_000


def _payment_fingerprint(header_value: str) -> str:
    import hashlib
    return hashlib.sha256(header_value.encode()).hexdigest()


def _claim_payment(payment_id: str) -> bool:
    """Atomically claim a payment id. False => already seen (replay)."""
    if payment_id in _seen_payments:
        return False
    if len(_seen_payments) >= _SEEN_PAYMENTS_MAX:
        # bounded memory: drop the set (oldest entries untracked) rather
        # than leak; real deployments use an LRU or persistent store.
        _seen_payments.clear()
    _seen_payments.add(payment_id)
    return True


def _facilitator_base() -> str:
    return os.environ.get(
        "TRYX402_FACILITATOR_URL",
        "https://x402.org/facilitator",  # CDP's public facilitator default
    ).rstrip("/")


def _facilitator_user_agent() -> str:
    """UA for facilitator calls. The public facilitator sits behind
    Cloudflare bot protection which 403s the default python-urllib UA."""
    return "tryx402-facade/1.0 (https://tryx402.fly.dev)"


def verify_with_facilitator(payment_payload: dict,
                            requirements: dict) -> dict:
    """POST /verify to the facilitator. Returns its JSON verdict.

    Raises FacilitatorError on any non-200 or invalid=false verdict.
    Never retries: a verification attempt does not move funds, but the
    follow-up settle does — same no-retry discipline as paid calls.
    """
    body = json.dumps({
        "x402Version": 1,
        "paymentPayload": payment_payload,
        "paymentRequirements": requirements,
    }).encode()
    req = urllib.request.Request(
        f"{_facilitator_base()}/verify",
        data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": _facilitator_user_agent()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            verdict = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            err_body = ""
        raise FacilitatorError(
            f"facilitator HTTP {exc.code}: {err_body or exc.reason}")
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

    # 1) Decode + verify the payment with the facilitator
    header = request.headers.get("X-PAYMENT", "")
    try:
        payment = decode_x402_header(header)
    except FacilitatorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # 1b) Replay protection: a given payment header is processed at most once.
    payment_id = _payment_fingerprint(header)
    if not _claim_payment(payment_id):
        raise HTTPException(status_code=409, detail={
            "error": "duplicate_payment",
            "message": ("this X-PAYMENT was already processed; if your first "
                        "attempt failed upstream, contact ops with this id "
                        "for reconciliation — never replay a paid header"),
            "payment_id": payment_id,
        })

    config = ProxyConfig(commission_rate=DEFAULT_COMMISSION_RATE,
                         min_commission_cents=DEFAULT_MIN_COMMISSION_CENTS)
    total_cents = config.calculate_total(price_cents)

    requirements = {
        "scheme": "exact",
        # facilitator /supported lists v1 kinds by legacy name too
        "network": os.environ.get("TRYX402_NETWORK", "base"),
        "maxAmountRequired": str(price_cents * 10_000),
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
            "x402Version": 1, "paymentPayload": payment,
            "paymentRequirements": requirements,
        }).encode()
        s_req = urllib.request.Request(f"{_facilitator_base()}/settle", data=s_body,
                                       headers={"Content-Type": "application/json",
                                                "User-Agent": _facilitator_user_agent()},
                                       method="POST")
        with urllib.request.urlopen(s_req, timeout=15) as s_resp:
            settlement = json.loads(s_resp.read())
    except Exception as exc:
        settlement = {"settled": False, "error": str(exc)}

    # 4) Sign a portable receipt (Ed25519) when a key is configured
    receipt = _build_receipt(
        endpoint=req.path, origin=req.origin, price_cents=price_cents,
        tx_hash=(settlement or {}).get("transaction") or (settlement or {}).get("txHash"),
        payer=payment.get("from"),
    )

    content = {
        "status_code": status_code,
        "body": body,
        "cost_cents": price_cents,
        "commission_cents": config.breakdown(price_cents)["commission_cents"],
        "total_atomic_units": requirements["maxAmountRequired"],
        "settlement": settlement,
    }
    if receipt is not None:
        content["receipt"] = receipt
    response = JSONResponse(status_code=200, content=content)
    if receipt is not None:
        response.headers["X-RECEIPT"] = "1"
    return response


# ---------------------------------------------------------------------------
# Receipt signing (module-level cached signer)
# ---------------------------------------------------------------------------

_receipt_builder = None


def _get_receipt_builder():
    """Lazily build the signer from TRYX402_RECEIPT_KEY (64 hex chars).

    Returns None when unconfigured: paid responses stay unsigned but are
    still served — reconciliation can rely on facilitator records.
    """
    global _receipt_builder
    if _receipt_builder is not None:
        return _receipt_builder
    from tryx402.receipts import ReceiptBuilder
    key_hex = os.environ.get("TRYX402_RECEIPT_KEY", "")
    if not key_hex:
        return None
    try:
        _receipt_builder = ReceiptBuilder(seed=bytes.fromhex(key_hex))
    except Exception:
        return None
    return _receipt_builder


def _build_receipt(*, endpoint: str, origin: str, price_cents: int,
                   tx_hash, payer):
    builder = _get_receipt_builder()
    if builder is None:
        return None
    try:
        return builder.build(
            endpoint=endpoint,
            origin=origin,
            price_usd=price_cents / 100.0,
            tx_hash=str(tx_hash) if tx_hash else None,
            account=payer,
        )
    except Exception:
        return None  # never break the paid path on receipt failure
