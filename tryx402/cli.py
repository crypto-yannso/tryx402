"""gateway CLI — the power-tool face of the SDK.

  gateway search "find work email"                      # discover endpoints by intent
  gateway discover https://stable-deepline.dev          # list an origin's endpoints
  gateway call <url> --body '{...}' --price 0.04         # safe call: budget + idempotency + ledger
  gateway quote <data_cost_usd>                          # ask the hosted service your price

Account management (create/fund/balances) lives on the hosted service at
https://www.tryx402.app — pricing is decided server-side, never in this CLI.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import catalog
from .client import AgentCashError, BudgetExceeded, SafeClient


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_search(a):
    for e in catalog.search(a.query, binary=a.agentcash, limit=a.limit):
        price = e.get("price_usd", e.get("price", "?"))
        print(f"{str(price):>12}  {e.get('method', 'POST'):5} "
              f"{e.get('origin', '')}{e.get('endpoint', e.get('path', ''))}")
        if e.get("summary"):
            print(f"              {e['summary'][:110]}")


def cmd_discover(a):
    for e in catalog.discover(a.origin, binary=a.agentcash):
        price = e.get("price_usd", e.get("price", "?"))
        print(f"{str(price):>12}  {e.get('method', 'POST'):5} "
              f"{e.get('endpoint', e.get('path', ''))}  {e.get('summary', '')[:80]}")


def cmd_call(a):
    body = json.loads(a.body) if a.body else None
    client = SafeClient(binary=a.agentcash, max_budget_usd=a.max_budget)
    try:
        data = client.call(a.url, method=a.method, body=body,
                           expected_price=a.price)
    except (BudgetExceeded, AgentCashError) as e:
        print(f"call failed: {e}", file=sys.stderr)
        sys.exit(1)

    spent = client.ledger.total_usd
    print(f"[cost] ${spent:.4f} upstream")
    _print_json(data)


def cmd_quote(a):
    """Ask the hosted service what a call costs (server-side FX + margin)."""
    api_base = os.environ.get("TRYX402_API", "https://tryx402.fly.dev").rstrip("/")
    api_key = os.environ.get("TRYX402_API_KEY")
    if not api_key:
        print("quote needs TRYX402_API_KEY (get one at https://www.tryx402.app)",
              file=sys.stderr)
        sys.exit(2)
    import urllib.request
    req = urllib.request.Request(
        f"{api_base}/v1/quote?data_cost_usd={a.data_cost_usd}",
        headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            _print_json(json.loads(r.read().decode()))
    except Exception as e:
        print(f"quote failed: {e}", file=sys.stderr)
        sys.exit(1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="gateway",
                                description="Safe x402 gateway — budget caps, idempotency, cost ledger.")
    p.add_argument("--agentcash", default=None,
                   help="override agentcash binary (default: 'agentcash' or 'npx agentcash@latest')")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(fn=cmd_search)

    d = sub.add_parser("discover")
    d.add_argument("origin")
    d.set_defaults(fn=cmd_discover)

    c = sub.add_parser("call")
    c.add_argument("url")
    c.add_argument("--body", default=None, help="JSON request body")
    c.add_argument("--method", default="POST")
    c.add_argument("--price", type=float, default=None, help="expected USD price (budget guard)")
    c.add_argument("--max-budget", type=float, default=None, help="hard USD cap for this call")
    c.set_defaults(fn=cmd_call)

    q = sub.add_parser("quote")
    q.add_argument("data_cost_usd", type=float,
                   help="upstream USD cost to price against your account")
    q.set_defaults(fn=cmd_quote)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
