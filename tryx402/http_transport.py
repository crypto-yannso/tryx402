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

import hashlib
import json

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


def price_from_402(body_bytes: bytes | None) -> float | None:
    """Extract maxAmountRequired from a 402 body when possible.

    Atomic-USDC heuristic per KEY: `maxAmountRequired` carries atomic units,
    so >10_000 means atomic → divide by 1e6; smaller values are display USD.
    Returns None when unparseable (unknown cost, never guessed).
    """
    if not body_bytes:
        return None
    try:
        data = json.loads(body_bytes.decode())
    except Exception:
        return None
    accepts = data.get("accepts") or []
    if not accepts:
        return None
    raw = accepts[0].get("maxAmountRequired")
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val / 1_000_000 if val > 10_000 else val


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
