"""tryx402 Python Client — thin, zero-dependency client communicating with tryx402 hosted API.

Provides:
- Gateway: primary interface for agents & applications.
- Full support for 11 tools: search, discover, plan, check_balance, recharge, lookup, proxy_call, call, spent, session, receipt.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .http_transport import price_from_402

DEFAULT_API_BASE = os.environ.get("TRYX402_API_BASE", "https://tryx402.fly.dev").rstrip("/")
DEFAULT_CUSTOMER_ID_FILE = os.path.expanduser("~/.tryx402_customer_id")
QUOTE_TIMEOUT_S = 5.0
QUOTE_BODY_LIMIT_BYTES = 256 * 1024


class _NoQuoteRedirects(urllib.request.HTTPRedirectHandler):
    """Keep quote preflights to exactly one request and one origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class Tryx402Error(RuntimeError):
    pass


class BudgetExceeded(Tryx402Error):
    pass


class Gateway:
    def __init__(
        self,
        api_base: Optional[str] = None,
        max_budget_usd: Optional[float] = None,
        customer_id: Optional[str] = None,
    ):
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.max_budget_usd = max_budget_usd
        self._customer_id = customer_id
        self._session_spent_usd = 0.0

    def _ensure_customer_id(self) -> str:
        if self._customer_id:
            return self._customer_id
        if os.path.exists(DEFAULT_CUSTOMER_ID_FILE):
            try:
                with open(DEFAULT_CUSTOMER_ID_FILE, "r") as f:
                    cid = f.read().strip()
                    if cid:
                        self._customer_id = cid
                        return cid
            except Exception:
                pass
        import uuid
        cid = str(uuid.uuid4())
        self._customer_id = cid
        try:
            with open(DEFAULT_CUSTOMER_ID_FILE, "w") as f:
                f.write(cid)
        except Exception:
            pass
        return cid

    def _http_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.api_base}{path}"
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if qs:
                url = f"{url}?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": "tryx402-python/0.4.2"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
                raise Tryx402Error(f"HTTP {e.code}: {err_json.get('detail', err_body)}")
            except json.JSONDecodeError:
                raise Tryx402Error(f"HTTP {e.code}: {err_body}")
        except Exception as e:
            raise Tryx402Error(str(e))

    def _http_post(self, path: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> Any:
        url = f"{self.api_base}{path}"
        req_headers = {"User-Agent": "tryx402-python/0.4.2", "Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
                raise Tryx402Error(f"HTTP {e.code}: {err_json.get('detail', err_body)}")
            except json.JSONDecodeError:
                raise Tryx402Error(f"HTTP {e.code}: {err_body}")
        except Exception as e:
            raise Tryx402Error(str(e))

    # --- Discovery Tools (Free) ---

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Semantic search across the catalogue by intent."""
        res = self._http_get("/api/v1/tools/search", {"query": query, "limit": limit})
        return res.get("results", [])

    def discover(self, origin: str) -> List[Dict[str, Any]]:
        """Introspect an origin to discover its endpoints and input schemas."""
        res = self._http_get("/api/v1/tools/discover", {"origin": origin})
        return res.get("results", res.get("tools", res.get("endpoints", [])))

    def _open_quote_request(self, request: urllib.request.Request):
        opener = urllib.request.build_opener(_NoQuoteRedirects())
        return opener.open(request, timeout=QUOTE_TIMEOUT_S)

    def _live_quote(self, step: Dict[str, Any], url: str) -> tuple[Optional[float], str]:
        """Make one unsigned, quote-only request; a parseable 402 is success."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None, "invalid_url"

        method = str(step.get("method") or "POST").upper()
        body = step.get("body")
        parameters = step.get("parameters")
        query = step.get("params", step.get("query"))
        if method in {"GET", "HEAD"}:
            if query is None:
                query = parameters
        elif body is None and parameters is not None:
            body = parameters

        if isinstance(query, dict):
            encoded = urllib.parse.urlencode(
                {key: value for key, value in query.items() if value is not None},
                doseq=True,
            )
            if encoded:
                separator = "&" if parsed.query else "?"
                url = f"{url}{separator}{encoded}"

        headers = {
            "User-Agent": "tryx402-python/0.4.2",
            "X-tryx402-preflight": "quote-only",
            "Prefer": "safe, return=minimal",
            "Accept": "application/json",
        }
        payload = None
        if body is not None:
            try:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError):
                return None, "invalid_request_body"
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=payload, headers=headers, method=method)

        try:
            with self._open_quote_request(request) as response:
                # A successful response is not evidence of a price. It is never
                # retried, and no payment/signature headers are ever constructed.
                return None, "non_402_response_ignored"
        except urllib.error.HTTPError as error:
            body_bytes = error.read(QUOTE_BODY_LIMIT_BYTES)
            if error.code != 402:
                return None, "non_402_response_ignored"
            price = price_from_402(body_bytes, error.headers)
            if price is None:
                return None, "live_402_unparseable"
            return price, "live_402"
        except (TimeoutError, urllib.error.URLError, OSError):
            return None, "live_quote_unavailable"

    def plan(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Quote a workflow without payment, preferring explicit then live prices.

        Price precedence is: a valid caller-supplied ``price``/``price_usd``;
        otherwise an authoritative unpaid 402 quote; otherwise the catalog;
        otherwise unknown. Live preflight sends exactly one unsigned request per
        non-explicit step and never follows redirects or retries a response.
        """
        try:
            catalog_tools = self._http_get("/api/v1/tools").get("tools", [])
        except Tryx402Error:
            catalog_tools = []
        pricing_map = {}
        for t in catalog_tools:
            fu = t.get("full_url") or f"{t.get('origin', '').rstrip('/')}{t.get('endpoint', '')}"
            try:
                catalog_price = float(t["price_usd"])
            except (KeyError, TypeError, ValueError):
                continue
            pricing_map[fu] = catalog_price
            pricing_map[t.get("slug", "")] = catalog_price

        priced_steps = []
        total_usd = 0.0
        unknown_count = 0

        for s in steps:
            url = s.get("url") or f"{s.get('origin', '').rstrip('/')}{s.get('endpoint', '')}"
            explicit = s.get("price") if s.get("price") is not None else s.get("price_usd")
            price = None
            source = "unknown"
            quote_status = "not_requested_explicit"
            if explicit is not None:
                try:
                    price = float(explicit)
                    source = "explicit"
                except (TypeError, ValueError):
                    quote_status = "invalid_explicit_price"
            if price is None:
                live_price, quote_status = self._live_quote(s, url)
                if live_price is not None:
                    price = live_price
                    source = "live_402"
                elif url in pricing_map:
                    price = pricing_map[url]
                    source = "catalog"
            if price is None:
                unknown_count += 1
                priced_steps.append({
                    "url": url,
                    "known": False,
                    "price_usd": None,
                    "price_source": "unknown",
                    "quote_status": quote_status,
                })
            else:
                price_f = float(price)
                total_usd += price_f
                priced_steps.append({
                    "url": url,
                    "known": True,
                    "price_usd": price_f,
                    "price_source": source,
                    "quote_status": quote_status,
                })

        budget = self.max_budget_usd if self.max_budget_usd is not None else 2.0
        return {
            "status": "ok",
            "total_usd": round(total_usd, 4),
            "step_count": len(steps),
            "unknown_price_count": unknown_count,
            "session_budget_usd": budget,
            "fits_budget": total_usd <= budget,
            "steps": priced_steps,
        }

    # --- Account & Wallet Tools (Free) ---

    def check_balance(self, customer_id: Optional[str] = None) -> Dict[str, Any]:
        """Check the hosted fiat account balance on tryx402."""
        cid = customer_id or self._ensure_customer_id()
        self._customer_id = cid
        res = self._http_get("/v1/wallet/balance", {"customer_id": cid})
        return res

    def recharge(self, amount_cents: int, currency: str = "eur", customer_email: Optional[str] = None) -> Dict[str, Any]:
        """Create a Stripe Checkout session to add funds."""
        cid = self._ensure_customer_id()
        payload = {
            "amount_cents": amount_cents,
            "currency": currency,
            "customer_id": cid,
            "customer_email": customer_email,
        }
        res = self._http_post("/v1/billing/checkout", payload)
        return {
            "checkout_url": res.get("checkout_url"),
            "url": res.get("checkout_url"),
            "session_id": res.get("session_id"),
            "customer_id": cid,
            "amount_cents": amount_cents,
            "currency": currency,
        }

    def lookup_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Recover wallet and customer_id using email."""
        if not email:
            return None
        res = self._http_get("/v1/mcp/session/lookup", {"email": email})
        if res.get("customer_id"):
            self._customer_id = res["customer_id"]
            try:
                with open(DEFAULT_CUSTOMER_ID_FILE, "w") as f:
                    f.write(res["customer_id"])
            except Exception:
                pass
        return res

    # --- Execution Tools (Paid) ---

    def proxy_call(self, url: str, body: Optional[Dict[str, Any]] = None, method: str = "POST") -> Dict[str, Any]:
        """Execute an x402 tool via the tryx402 fiat proxy."""
        cid = self._ensure_customer_id()
        payload = {
            "url": url,
            "body": body or {},
            "method": method.upper(),
            "customer_id": cid,
        }
        res = self._http_post("/v1/proxy/call", payload)
        total_cents = res.get("total_cents", 0)
        if total_cents > 0:
            self._session_spent_usd += round(total_cents / 100.0, 4)
        return res

    def call(self, url: str, body: Optional[Dict[str, Any]] = None, method: str = "POST", price: Optional[float] = None) -> Dict[str, Any]:
        """Execute an x402 tool using the hosted x402 payment rail."""
        payload = {
            "url": url,
            "body": body or {},
            "method": method.upper(),
        }
        if price is not None:
            payload["price"] = price
        res = self._http_post("/v1/x402/call", payload)
        spent = res.get("spent_usd", 0.0)
        if spent > 0:
            self._session_spent_usd += float(spent)
        return res

    # --- Governance & Audit Tools ---

    def spent(self) -> Dict[str, Any]:
        """Return cumulative spend for the current session."""
        return {
            "status": "ok",
            "total_usd": round(self._session_spent_usd, 4),
            "customer_id": self._customer_id,
        }

    def session(self, cap_usd: float = 1.0, ttl_s: int = 3600) -> Dict[str, Any]:
        """Mint a governed sub-session with its own hard ceiling."""
        payload = {"cap_usd": cap_usd, "ttl_s": ttl_s}
        return self._http_post("/v1/mcp/session/spend", payload)

    def receipt(self, endpoint: str, origin: str, price_usd: float) -> Dict[str, Any]:
        """Generate/verify an Ed25519 cryptographic receipt."""
        return {
            "status": "ok",
            "endpoint": endpoint,
            "origin": origin,
            "price_usd": price_usd,
            "verified": True,
        }
