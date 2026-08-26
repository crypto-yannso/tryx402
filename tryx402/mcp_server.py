"""Zero-dependency MCP server exposing the safe gateway as agent tools.

This is the most agent-native embedding — same idea as AgentCash's own MCP, but
every call goes through the safe engine (hard budget cap, idempotency, ledger).
No SDK: plain JSON-RPC 2.0 over newline-delimited stdio.

Add it to an agent's MCP config as a simple command:

    {
      "command": "python3",
      "args": ["-m", "gateway.mcp_server"],
      "env": { "GATEWAY_MAX_BUDGET_USD": "1.00" }
    }

Tools: gateway_search, gateway_discover, gateway_call, gateway_spent.
gateway_call SPENDS real USDC (budget-capped). A single Gateway instance lives
for the whole session, so the budget and idempotency cache span all calls.
"""
from __future__ import annotations

import json
import os
import sys

from .api import Gateway

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "tryx402", "version": "0.4.0"}


def _budget():
    v = os.environ.get("TRYX402_MAX_BUDGET_USD") or os.environ.get("GATEWAY_MAX_BUDGET_USD")
    return float(v) if v else None


_GW = Gateway(max_budget_usd=_budget())

TOOLS = [
    {"name": "gateway_search",
     "description": "Find AgentCash/x402 endpoints across the whole catalogue by "
                    "natural-language intent. Free (no payment).",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "gateway_discover",
     "description": "List one origin's endpoints with prices. Free (no payment).",
     "inputSchema": {"type": "object",
                     "properties": {"origin": {"type": "string"}},
                     "required": ["origin"]}},
    {"name": "gateway_call",
     "description": "Call a pay-per-use API endpoint safely: budget cap, idempotency, "
                    "cost ledger. Billed in USD. Pass `price` (expected USD) "
                    "so budget tracking works.",
     "inputSchema": {"type": "object",
                     "properties": {"url": {"type": "string"},
                                    "body": {"type": "object"},
                                    "method": {"type": "string"},
                                    "price": {"type": "number"}},
                     "required": ["url"]}},
    {"name": "gateway_plan",
     "description": "Price an entire multi-call workflow BEFORE spending. Free. "
                    "Pass steps: [{url, body?, price?}]. Returns total, per-step "
                    "costs, unknown-price flags, and whether it fits the budget.",
     "inputSchema": {"type": "object",
                     "properties": {"steps": {"type": "array",
                                              "items": {"type": "object"}}},
                     "required": ["steps"]}},
    {"name": "gateway_receipt",
     "description": "Sign a verifiable receipt (Ed25519) for a paid call. Free.",
     "inputSchema": {"type": "object",
                     "properties": {"endpoint": {"type": "string"},
                                    "origin": {"type": "string"},
                                    "price_usd": {"type": "number"},
                                    "tx_hash": {"type": "string"}},
                     "required": ["endpoint", "origin", "price_usd"]}},
    {"name": "gateway_spent",
     "description": "How much this session has spent (USD), total and by origin.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _handle_tool(name, args):
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
    if name == "gateway_plan":
        from .planner import estimate_plan
        est = estimate_plan(args.get("steps") or [],
                            spent_usd=_GW.spent_usd,
                            max_budget_usd=_GW._client.max_budget_usd)
        return json.dumps(est.to_dict(), ensure_ascii=False, indent=2)
    if name == "gateway_receipt":
        r = _GW.receipt(args["endpoint"], args["origin"],
                        args["price_usd"], tx_hash=args.get("tx_hash"))
        return json.dumps({"receipt": r, "verify_hint": "verify with the "
                           "pubkey embedded in the receipt (offline)"},
                          ensure_ascii=False, indent=2)
    if name == "gateway_spent":
        return json.dumps({"total_usd": _GW.spent_usd, "by_origin": _GW.spend_by_origin()},
                          indent=2)
    raise ValueError(f"unknown tool: {name}")


class _MethodNotFound(Exception):
    pass


def _dispatch(req):
    method = req.get("method")
    if method == "initialize":
        params = req.get("params") or {}
        return {"protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        p = req.get("params") or {}
        try:
            text = _handle_tool(p.get("name"), p.get("arguments") or {})
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
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
