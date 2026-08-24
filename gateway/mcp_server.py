"""Zero-dependency MCP server exposing the safe gateway as agent tools.

This is the most agent-native embedding — same idea as AgentCash's own MCP, but
every call goes through the safe engine (hard budget cap, idempotency, ledger).
No SDK: plain JSON-RPC 2.0 over newline-delimited stdio.

Add it to an agent's MCP config as a simple command:

    {
      "command": "python3",
      "args": ["-m", "gateway.mcp_server"],
      "env": { "GATEWAY_MAX_BUDGET_USD": "1.00", "TRYX402_API_KEY": "***" }
    }

Tools (0.3.1): gateway_search, gateway_discover, gateway_call, gateway_spent,
gateway_plan, gateway_receipt, gateway_session, gateway_check_balance,
gateway_recharge.
gateway_call SPENDS real USDC (budget-capped). A single Gateway instance lives
for the whole session, so the budget and idempotency cache span all calls.
"""
from __future__ import annotations

import json
import os
import sys

from .api import Gateway

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "tryx402", "version": "0.3.1"}


def _budget():
    v = os.environ.get("GATEWAY_MAX_BUDGET_USD")
    return float(v) if v else None


_GW = Gateway(max_budget_usd=_budget())

TOOLS = [
    {
        "name": "gateway_search",
        "description": "Find AgentCash/x402 endpoints across the whole catalogue by "
                       "natural-language intent. Free (no payment).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "gateway_discover",
        "description": "List one origin's endpoints with prices. Free (no payment).",
        "inputSchema": {
            "type": "object",
            "properties": {"origin": {"type": "string"}},
            "required": ["origin"]
        }
    },
    {
        "name": "gateway_call",
        "description": "Call ANY AgentCash/x402 endpoint safely: hard budget cap, "
                       "idempotency, cost ledger. SPENDS real USDC. Pass `price` "
                       "(expected USD) so the budget cap and accounting work.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "body": {"type": "object"},
                "method": {"type": "string"},
                "price": {"type": "number"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "gateway_spent",
        "description": "How much this session has spent (USD), total and by origin.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "gateway_plan",
        "description": "Estimate the cost of a multi-call workflow BEFORE spending. "
                       "No wallet activity. Returns whether the plan fits the budget.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "steps": {"type": "array"},
                "max_budget_usd": {"type": "number"}
            },
            "required": ["steps"]
        }
    },
    {
        "name": "gateway_receipt",
        "description": "Sign a verifiable Ed25519 receipt for a paid call. "
                       "Proof of payment, checkable offline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "origin": {"type": "string"},
                "price_usd": {"type": "number"},
                "tx_hash": {"type": "string"}
            },
            "required": ["endpoint", "origin", "price_usd"]
        }
    },
    {
        "name": "gateway_session",
        "description": "Mint a governed sub-session with its own spend cap + circuit breaker. "
                       "Blast radius per task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cap_usd": {"type": "number"},
                "ttl_s": {"type": "integer"}
            },
            "required": []
        }
    },
    {
        "name": "gateway_check_balance",
        "description": "Check wallet balance on the hosted service (tryx402.fly.dev). "
                       "Returns balance in cents. Requires API key.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "gateway_recharge",
        "description": "Create a Stripe Checkout session to top up the wallet. "
                       "Returns a URL to redirect the user to. Requires API key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount_cents": {"type": "integer"},
                "currency": {"type": "string"}
            },
            "required": ["amount_cents"]
        }
    },
    {
        "name": "gateway_proxy_call",
        "description": "Call an x402 endpoint through the tryx402 proxy. "
                       "Debits wallet with commission (default 10%). "
                       "No API key needed: uses anonymous customer ID. "
                       "This is how tryx402 makes money.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "body": {"type": "object"},
                "method": {"type": "string"},
                "price_usd": {"type": "number"}
            },
            "required": ["url", "price_usd"]
        }
    },
]


def _handle_tool(name, args):
    try:
        if name == "gateway_search":
            return json.dumps(_GW.search(args["query"], limit=int(args.get("limit", 10))),
                              ensure_ascii=False, indent=2)
        if name == "gateway_discover":
            return json.dumps(_GW.discover(args["origin"]), ensure_ascii=False, indent=2)
        if name == "gateway_call":
            data = _GW.call(args["url"], body=args.get("body"),
                            method=args.get("method", "POST"), price=args.get("price"))
            return json.dumps({"spent_usd": _GW.spent_usd, "data": data},
                              ensure_ascii=False, indent=2)
        if name == "gateway_spent":
            return json.dumps({"total_usd": _GW.spent_usd, "by_origin": _GW.spend_by_origin()},
                              indent=2)
        if name == "gateway_plan":
            steps = args.get("steps", [])
            plan = _GW.plan(steps)
            return json.dumps(plan, ensure_ascii=False, indent=2)
        if name == "gateway_receipt":
            r = _GW.receipt(
                endpoint=args["endpoint"],
                origin=args["origin"],
                price_usd=float(args["price_usd"]),
                tx_hash=args.get("tx_hash"),
            )
            return json.dumps(r, ensure_ascii=False, indent=2)
        if name == "gateway_session":
            cap = args.get("cap_usd")
            ttl = int(args.get("ttl_s", 3600))
            sess = _GW.session(cap_usd=cap, ttl_s=ttl)
            return json.dumps({"session_token": sess.token}, indent=2)
        if name == "gateway_check_balance":
            bal = _GW.check_balance()
            if bal is None:
                return json.dumps({"error": "No API key configured"}, indent=2)
            return json.dumps(bal, ensure_ascii=False, indent=2)
        if name == "gateway_recharge":
            amount = int(args.get("amount_cents", 0))
            currency = args.get("currency", "eur")
            result = _GW.recharge(amount_cents=amount, currency=currency)
            return json.dumps(result, ensure_ascii=False, indent=2)
        if name == "gateway_proxy_call":
            url = args.get("url", "")
            body = args.get("body")
            method = args.get("method", "POST")
            price_usd = float(args.get("price_usd", 0))
            result = _GW.proxy_call(url, body=body, method=method, price_usd=price_usd)
            return json.dumps(result, ensure_ascii=False, indent=2)
        raise ValueError(f"unknown tool: {name}")
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)}, indent=2)


class _MethodNotFound(Exception):
    pass


def _dispatch(req):
    method = req.get("method")
    if method == "initialize":
        params = req.get("params") or {}
        return {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        p = req.get("params") or {}
        text = _handle_tool(p.get("name"), p.get("arguments") or {})
        try:
            parsed = json.loads(text)
            is_error = "error" in parsed
        except Exception:  # noqa: BLE001
            is_error = True
            parsed = {"error": text}
        return {"content": [{"type": "text", "text": text}], "isError": is_error}
    if method == "ping":
        return {}
    raise _MethodNotFound(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        if rid is None:
            continue                      # a notification — no response
        try:
            resp = {"jsonrpc": "2.0", "id": rid, "result": _dispatch(req)}
        except _MethodNotFound as e:
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"method not found: {e}"}}
        except Exception as e:            # noqa: BLE001
            resp = {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
