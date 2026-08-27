"""MCP server for tryx402 — clean, pure HTTP proxy server.

Exposes the 11 tryx402 capabilities via standard MCP stdio JSON-RPC.
All operations are safely forwarded to the hosted tryx402 API.
Zero local secrets, zero private databases.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

from .api import Gateway

_GW: Gateway | None = None


def _get_gateway() -> Gateway:
    global _GW
    if _GW is None:
        budget_str = os.environ.get("TRYX402_MAX_BUDGET_USD", "2.00")
        try:
            budget = float(budget_str)
        except ValueError:
            budget = 2.00
        _GW = Gateway(max_budget_usd=budget)
    return _GW


TOOLS = [
    {
        "name": "gateway_search",
        "description": (
            "Search the VERIFIED tryx402 tool catalogue for pay-per-use endpoints. "
            "Supports natural language and intent search in French and English."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or task description"},
                "limit": {"type": "integer", "description": "Maximum number of tools to return (default: 10)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "gateway_discover",
        "description": "Deep-dive into a single provider origin: lists every x402 endpoint with price and exact input schema.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "Provider origin URL (e.g. https://api.paywithlocus.com)"},
            },
            "required": ["origin"],
        },
    },
    {
        "name": "gateway_plan",
        "description": "Estimate the TOTAL cost of a multi-step workflow BEFORE executing. Free and instantaneous.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "origin": {"type": "string"},
                            "endpoint": {"type": "string"},
                            "url": {"type": "string"},
                            "price": {"type": "number"},
                            "price_usd": {"type": "number"},
                        },
                    },
                    "description": "List of steps to price",
                },
            },
            "required": ["steps"],
        },
    },
    {
        "name": "gateway_check_balance",
        "description": "Check the HOSTED FIAT account balance on tryx402. Returns balance in cents, formatted currency, and customer_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "Optional customer ID to inspect specific wallet"},
            },
        },
    },
    {
        "name": "gateway_recharge",
        "description": (
            "Create a STRIPE CHECKOUT session to add funds to the hosted fiat account. "
            "Returns checkout URL and customer_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount_cents": {"type": "integer", "description": "Amount to recharge in cents (e.g. 500 = $5.00 / 5.00 EUR)"},
                "currency": {"type": "string", "description": "Currency code (default: eur, supports usd)", "default": "eur"},
                "customer_email": {"type": "string", "description": "User's email for Stripe receipt and wallet binding"},
            },
            "required": ["amount_cents"],
        },
    },
    {
        "name": "gateway_lookup",
        "description": "Recover a customer's wallet and balance using their email address.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "The user email used during Stripe Checkout"},
            },
            "required": ["email"],
        },
    },
    {
        "name": "gateway_proxy_call",
        "description": (
            "Execute an x402 endpoint through the tryx402 FIAT PROXY. Debits the hosted balance. "
            "Backed by automatic 100% refund if provider fails."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to call"},
                "body": {"type": "object", "description": "JSON request payload"},
                "method": {"type": "string", "description": "HTTP method (POST, GET, etc.)", "default": "POST"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "gateway_call",
        "description": "Execute a pay-per-use x402 API call through the hosted x402 rail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full target endpoint URL"},
                "method": {"type": "string", "description": "HTTP method", "default": "POST"},
                "body": {"type": "object", "description": "Request body JSON payload"},
                "price": {"type": "number", "description": "Expected price in USD"},
            },
            "required": ["url"],
        },
    },
    {
        "name": "gateway_spent",
        "description": "Return the cumulative spend for the CURRENT session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "gateway_session",
        "description": "Mint a GOVERNED SUB-SESSION with its own hard spending cap.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cap_usd": {"type": "number", "description": "Hard spending ceiling in USD (default: 1.00)"},
                "ttl_s": {"type": "integer", "description": "Session lifetime in seconds (default: 3600)"},
            },
        },
    },
    {
        "name": "gateway_receipt",
        "description": "Generate or verify a cryptographically signed Ed25519 receipt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "origin": {"type": "string"},
                "price_usd": {"type": "number"},
            },
            "required": ["endpoint", "origin", "price_usd"],
        },
    },
]


def _handle_tool(name: str, args: Dict[str, Any]) -> str:
    gw = _get_gateway()
    try:
        if name == "gateway_search":
            q = args.get("query", "")
            limit = int(args.get("limit", 10))
            return json.dumps({"status": "ok", "query": q, "count": len(gw.search(q, limit)), "results": gw.search(q, limit)}, ensure_ascii=False)
        elif name == "gateway_discover":
            orig = args.get("origin", "")
            eps = gw.discover(orig)
            return json.dumps({"status": "ok", "origin": orig, "count": len(eps), "endpoints": eps}, ensure_ascii=False)
        elif name == "gateway_plan":
            steps = args.get("steps", [])
            return json.dumps(gw.plan(steps), ensure_ascii=False)
        elif name == "gateway_check_balance":
            cid = args.get("customer_id")
            return json.dumps(gw.check_balance(cid), ensure_ascii=False)
        elif name == "gateway_recharge":
            amt = int(args.get("amount_cents", 0))
            curr = args.get("currency", "eur")
            email = args.get("customer_email")
            return json.dumps(gw.recharge(amt, curr, email), ensure_ascii=False)
        elif name == "gateway_lookup":
            em = args.get("email", "")
            return json.dumps(gw.lookup_by_email(em) or {"status": "not_found", "message": "No account found"}, ensure_ascii=False)
        elif name == "gateway_proxy_call":
            u = args.get("url", "")
            b = args.get("body")
            m = args.get("method", "POST")
            return json.dumps(gw.proxy_call(u, b, m), ensure_ascii=False)
        elif name == "gateway_call":
            u = args.get("url", "")
            b = args.get("body")
            m = args.get("method", "POST")
            p = args.get("price")
            return json.dumps(gw.call(u, b, m, p), ensure_ascii=False)
        elif name == "gateway_spent":
            return json.dumps(gw.spent(), ensure_ascii=False)
        elif name == "gateway_session":
            cap = float(args.get("cap_usd", 1.0))
            ttl = int(args.get("ttl_s", 3600))
            return json.dumps(gw.session(cap, ttl), ensure_ascii=False)
        elif name == "gateway_receipt":
            ep = args.get("endpoint", "")
            orig = args.get("origin", "")
            pu = float(args.get("price_usd", 0.0))
            return json.dumps(gw.receipt(ep, orig, pu), ensure_ascii=False)
        else:
            return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e), "status": "failed"})


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "tryx402", "version": "0.4.1"},
                },
            }
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
        elif method == "tools/call":
            t_name = params.get("name", "")
            t_args = params.get("arguments", {})
            res_str = _handle_tool(t_name, t_args)
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": res_str}]},
            }
        else:
            resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
