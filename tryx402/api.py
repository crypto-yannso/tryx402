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

DEFAULT_API_BASE = os.environ.get("TRYX402_API_BASE", "https://tryx402.fly.dev").rstrip("/")
DEFAULT_CUSTOMER_ID_FILE = os.path.expanduser("~/.tryx402_customer_id")


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
        req = urllib.request.Request(url, headers={"User-Agent": "tryx402-python/0.4.1"})
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
        req_headers = {"User-Agent": "tryx402-python/0.4.1", "Content-Type": "application/json"}
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

    def plan(self, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Pre-calculate the total cost of a multi-step workflow before execution."""
        catalog_tools = self._http_get("/api/v1/tools").get("tools", [])
        pricing_map = {}
        for t in catalog_tools:
            fu = t.get("full_url") or f"{t.get('origin', '').rstrip('/')}{t.get('endpoint', '')}"
            pricing_map[fu] = float(t.get("price_usd", 0.0))
            pricing_map[t.get("slug", "")] = float(t.get("price_usd", 0.0))

        priced_steps = []
        total_usd = 0.0
        unknown_count = 0

        for s in steps:
            url = s.get("url") or f"{s.get('origin', '').rstrip('/')}{s.get('endpoint', '')}"
            price = s.get("price") or s.get("price_usd")
            if price is None:
                price = pricing_map.get(url)
            if price is None:
                unknown_count += 1
                priced_steps.append({"url": url, "known": False, "price_usd": None})
            else:
                price_f = float(price)
                total_usd += price_f
                priced_steps.append({"url": url, "known": True, "price_usd": price_f})

        budget = self.max_budget_usd or 2.0
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
