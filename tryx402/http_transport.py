"""Transparent 402→pay→retry transport wrapper (x402-anthropic pattern).

Wraps an httpx transport so that a 402 Payment Required response is answered
AUTOMATICALLY: the payment header is built by a pluggable payer callback, the
request is retried once with it, and every paid call goes through the same
budget/ledger discipline as the CLI path.

The x402-anthropic projects proved the pattern on one SDK; here it is generic:

    from tryx402.http_transport import X402Transport

    transport = X402Transport(payer=my_payer, max_budget_usd=1.0)
    with httpx.Client(transport=transport) as client:
        r = client.post("https://seller.example/api", json={...})
        # 402 → signed payment header → auto-retry → 200

`payer` signature: payer(request_dict, challenge_dict) -> (header_name, value).
Any wallet plugs in (native x402 SDK signer, AgentCash CLI, ...); governance
(budget cap, ledger, single-retry discipline) stays ours.

NO silent double-pay: exactly ONE retry after a 402. A 402 on the RETRY is an
error, not a loop. A failure after payment is surfaced as PaidCallTimeout so
callers reconcile instead of blindly retrying (the $0.83 lesson).
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping

from .ledger import CostEvent, Ledger

try:
    import httpx
except ImportError:                    # zero-dependency install stays importable
    httpx = None


class TransportError(RuntimeError):
    pass


class PaidCallTimeout(TransportError):
    """Request failed AFTER payment may have settled — do NOT auto-retry."""


def _idem(method: str, url: str, body) -> str:
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False) if body is not None else ""
    return hashlib.sha256(f"{method}|{url}|{payload}".encode()).hexdigest()[:32]


def _origin(url: str) -> str:
    parts = url.split("://", 1)
    host = parts[-1].split("/", 1)[0]
    return f"{parts[0] if len(parts) == 2 else 'https'}://{host}"


def _json_bytes(payload: bytes | str | None) -> Any:
    if not payload:
        return None
    try:
        if isinstance(payload, bytes):
            payload = payload.decode()
        return json.loads(payload)
    except Exception:
        return None


def _header_value(headers: Mapping[str, Any] | None, name: str) -> str | None:
    if not headers:
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return value.decode() if isinstance(value, bytes) else str(value)
    return None


def _decode_challenge_header(value: str | None) -> Any:
    if not value:
        return None
    direct = _json_bytes(value)
    if direct is not None:
        return direct
    try:
        padded = value + "=" * (-len(value) % 4)
        return _json_bytes(base64.urlsafe_b64decode(padded.encode()))
    except Exception:
        return None


def _asset_decimals(requirement: Mapping[str, Any]) -> int | None:
    containers = [
        requirement,
        requirement.get("extra"),
        requirement.get("metadata"),
        requirement.get("assetMetadata"),
        requirement.get("asset") if isinstance(requirement.get("asset"), dict) else None,
    ]
    for container in containers:
        if not isinstance(container, dict) or "decimals" not in container:
            continue
        try:
            decimals = int(container["decimals"])
        except (TypeError, ValueError):
            continue
        if 0 <= decimals <= 30:
            return decimals
    return None


def _requirement_price(requirement: Mapping[str, Any]) -> float | None:
    key = "amount" if requirement.get("amount") is not None else "maxAmountRequired"
    raw = requirement.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None

    decimals = _asset_decimals(requirement)
    if decimals is not None:
        return value / (10 ** decimals)

    # x402 v1 historically mixed display values and six-decimal stablecoin
    # atomic units. Keep the old conservative compatibility heuristic only
    # when the server omitted asset metadata.
    if value > 10_000:
        return value / 1_000_000
    return value


def _challenge_price(data: Any) -> float | None:
    if not isinstance(data, dict):
        return None
    accepts = data.get("accepts") or data.get("paymentRequirements") or []
    if isinstance(accepts, dict):
        accepts = [accepts]
    prices = [
        price for price in (
            _requirement_price(item) for item in accepts if isinstance(item, dict)
        ) if price is not None
    ]
    # Multiple payment options should normally be equivalent. Taking the
    # largest avoids understating a budget if a server publishes drifted ones.
    return max(prices) if prices else None


def _money_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str)):
        try:
            amount = float(value)
            return amount if amount >= 0 else None
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    currency = str(value.get("currency") or value.get("currencyCode") or "USD").upper()
    if currency not in {"USD", "USDC", "USDT"}:
        return None
    if value.get("units") is not None:
        try:
            return float(value["units"]) + float(value.get("nanos", 0)) / 1_000_000_000
        except (TypeError, ValueError):
            return None
    return _money_value(value.get("amount", value.get("value")))


def _checkout_price(data: Any) -> float | None:
    """Read display-denominated USD totals from common 402/UCP bodies."""
    if not isinstance(data, dict):
        return None
    roots = [data]
    for key in ("checkout", "checkoutSession", "checkout_session", "payment"):
        if isinstance(data.get(key), dict):
            roots.append(data[key])
    for root in roots:
        for key in ("total", "totalAmount", "total_amount", "totalPrice", "total_price"):
            if key in root:
                price = _money_value(root[key])
                if price is not None:
                    return price
        totals = root.get("totals")
        if isinstance(totals, list):
            for total in totals:
                if not isinstance(total, dict):
                    continue
                if str(total.get("type", "total")).lower() in {"total", "grand_total"}:
                    price = _money_value(total.get("amount", total))
                    if price is not None:
                        return price
    return None


def price_from_402(
    body_bytes: bytes | None,
    headers: Mapping[str, Any] | None = None,
) -> float | None:
    """Extract a USD quote from common x402 v1/v2 and UCP 402 forms.

    A ``payment-required`` base64 JSON header is authoritative when usable.
    Body challenges are next, followed by display-denominated checkout totals.
    Declared asset decimals are honored; malformed data remains unknown.
    """
    header = _header_value(headers, "payment-required")
    if header is None:
        header = _header_value(headers, "x-payment-required")
    header_data = _decode_challenge_header(header)
    price = _challenge_price(header_data)
    if price is not None:
        return price
    body_data = _json_bytes(body_bytes)
    price = _challenge_price(body_data)
    if price is not None:
        return price
    return _checkout_price(body_data)


def _event(endpoint: str, origin: str, price: float, paid: bool) -> CostEvent:
    return CostEvent(endpoint=endpoint, origin=origin, price_usd=price, paid=paid)


class _Core402:
    """Shared 402-governance logic used by both the httpx and raw paths."""

    def __init__(self, payer, max_budget_usd, ledger: Ledger):
        self.payer = payer
        self.max_budget_usd = max_budget_usd
        self.ledger = ledger

    def authorize(self, method: str, url: str, challenge_body: bytes | None):
        """Returns (header_name, header_value, price) or raises BudgetExceeded-ish."""
        if self.payer is None:
            return None
        price = price_from_402(challenge_body)
        if price is not None and self.max_budget_usd is not None \
                and self.ledger.total_usd + price > self.max_budget_usd + 1e-9:
            from .client import BudgetExceeded
            raise BudgetExceeded(
                f"budget cap ${self.max_budget_usd:.2f} reached "
                f"(spent ${self.ledger.total_usd:.2f}, next ~${price:.4f})")
        req_info = {"method": method, "url": url}
        hname, hval = self.payer(req_info, json.loads(challenge_body.decode())
                                 if challenge_body else {})
        return hname, hval, price

    def record(self, endpoint: str, origin: str, price: float | None, paid: bool):
        self.ledger.record(_event(endpoint, origin,
                                  price if price is not None else -1.0, paid))


if httpx is not None:

    class X402Transport(httpx.BaseTransport):  # type: ignore[misc]
        """httpx transport: pass-through until a 402, then pay + retry ONCE."""

        def __init__(self, payer=None, *, max_budget_usd: float | None = None,
                     ledger: Ledger | None = None, idempotent: bool = True):
            self.payer = payer
            self.idempotent = idempotent
            self.core = _Core402(payer, max_budget_usd, ledger or Ledger())
            self._inner = httpx.HTTPTransport()
            self._cache = {}

        def handle_request(self, request):
            from httpx import Request, Response

            body = request.content or None
            key = _idem(request.method, str(request.url), body)
            cached = self._cache.get(key)
            if cached is not None and self.idempotent:
                resp = Response(200, content=cached)
                resp.headers["x-tryx402-cache"] = "hit"
                return resp

            first = self._inner.handle_request(request)
            if first.status_code != 402:
                return first

            challenge = bytes(first.content or b"")
            auth = self.core.authorize(request.method, str(request.url), challenge)
            if auth is None:
                return first                       # no payer: 402 passes through
            hname, hval, price = auth

            retry_req = Request(request.method, request.url,
                                headers={**request.headers, hname: hval},
                                content=body)
            try:
                final = self._inner.handle_request(retry_req)
            except Exception as e:
                self.core.record(str(request.url.path), _origin(str(request.url)),
                                 price, paid=False)
                raise PaidCallTimeout(
                    f"{request.method} {request.url} failed after payment"
                    f" (~${price if price is not None else '?'}); reconcile") from e

            self.core.record(str(request.url.path), _origin(str(request.url)),
                             price, paid=final.status_code == 200)
            if final.status_code == 402:
                raise TransportError("402 again AFTER payment — refusing to loop")
            if self.idempotent:
                self._cache[key] = final.content
            out = Response(final.status_code, content=final.content,
                           headers=final.headers)
            return out

        def close(self):
            self._inner.close()


else:

    class X402Transport:                   # pragma: no cover — httpx absent
        def __init__(self, *a, **kw):
            raise TransportError("httpx not installed — pip install 'tryx402[native]'")


def pay_and_retry(method: str, url: str, *, headers: dict | None = None,
                  body=None, payer, opener,
                  max_budget_usd: float | None = None,
                  ledger: Ledger | None = None) -> dict:
    """httpx-free implementation of the same discipline for urllib callers.

    opener(method, url, headers, body_bytes) -> (status:int, headers:dict, content:bytes)
    Returns {status, headers, content}. Raises PaidCallTimeout on post-payment
    failure; refuses to loop on double-402.
    """
    core = _Core402(payer, max_budget_usd, ledger or Ledger())
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None

    status, rhdr, content = opener(method, url, headers or {}, payload)
    result = {"status": status, "headers": rhdr, "content": content}
    if status != 402 or core.payer is None:
        return result

    auth = core.authorize(method, url, content)
    if auth is None:
        return result
    hname, hval, price = auth

    try:
        status2, rhdr2, content2 = opener(method, url,
                                          {**(headers or {}), hname: hval}, payload)
    except Exception as e:
        core.record(url, _origin(url), price, paid=False)
        raise PaidCallTimeout(f"{method} {url} failed after payment; reconcile") from e

    core.record(url, _origin(url), price, paid=status2 == 200)
    if status2 == 402:
        raise TransportError("402 again AFTER payment — refusing to loop")
    return {"status": status2, "headers": rhdr2, "content": content2}
