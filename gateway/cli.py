"""gateway CLI — power-tool (option 3) that also drives the fiat gateway (option 2).

  gateway search "find work email"                      # discover endpoints by intent
  gateway discover https://stable-deepline.dev          # list an origin's endpoints
  gateway call <url> --body '{...}' --price 0.04         # safe call: budget + idempotency + ledger
  gateway account create acme --currency EUR --margin 0.30
  gateway account fund acme --amount 20                  # in the account's own currency
  gateway call <url> --body '{...}' --price 0.04 --account acme   # bill the call in that currency
"""
from __future__ import annotations

import argparse
import json
import sys

from . import catalog
from .accounts import (
    AccountStore, FxRates, InsufficientBalance, UnknownCurrency, format_amount, price_minor,
)
from .client import AgentCashError, BudgetExceeded, SafeClient


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_search(a):
    for e in catalog.search(a.query, binary=a.agentcash, limit=a.limit):
        print(f"{str(e.get('price') or '?'):>12}  {e.get('method', 'POST'):5} "
              f"{e.get('origin', '')}{e.get('path', '')}")
        if e.get("summary"):
            print(f"              {e['summary'][:110]}")


def cmd_discover(a):
    for e in catalog.discover(a.origin, binary=a.agentcash):
        print(f"{str(e.get('price') or '?'):>12}  {e.get('method', 'POST'):5} "
              f"{e.get('path', '')}  {e.get('summary', '')[:80]}")


def cmd_call(a):
    body = json.loads(a.body) if a.body else None
    client = SafeClient(binary=a.agentcash, max_budget_usd=a.max_budget)
    store, acct_id, rates = None, None, FxRates()

    if a.account:
        store = AccountStore.load(a.store)
        if a.account not in store.accounts:
            print(f"unknown account: {a.account}", file=sys.stderr)
            sys.exit(2)
        acct_id = a.account
        try:  # pre-authorize against the expected price, in the account's currency
            store.authorize(acct_id, a.price or 0.0, rates)
        except (InsufficientBalance, UnknownCurrency) as e:
            print(str(e), file=sys.stderr)
            sys.exit(3)

    try:
        data = client.call(a.url, method=a.method, body=body,
                           expected_price=a.price, account=acct_id)
    except (BudgetExceeded, AgentCashError) as e:
        print(f"call failed: {e}", file=sys.stderr)
        sys.exit(1)

    spent = client.ledger.total_usd
    if acct_id:
        acct = store.accounts[acct_id]
        billed = price_minor(spent, acct.currency, rates, acct.margin)
        store.charge(acct_id, billed)
        store.save()
        print(f"[cost] ${spent:.4f} upstream  ->  billed {format_amount(billed, acct.currency)} "
              f"to {acct_id} (margin {int(acct.margin * 100)}%, "
              f"balance {format_amount(acct.balance_minor, acct.currency)})")
    else:
        print(f"[cost] ${spent:.4f} upstream")
    _print_json(data)


def cmd_account(a):
    store = AccountStore.load(a.store)
    if a.sub == "create":
        store.create(a.id, currency=a.currency, margin=a.margin)
        store.save()
        print(f"account '{a.id}' created ({a.currency.upper()}, margin {int(a.margin * 100)}%)")
    elif a.sub == "fund":
        acct = store.fund(a.id, a.amount)
        store.save()
        print(f"funded '{a.id}': +{a.amount:g} {acct.currency}  "
              f"(balance {format_amount(acct.balance_minor, acct.currency)})  [Stripe stub]")
    elif a.sub == "status":
        ids = [a.id] if a.id else list(store.accounts)
        if not ids:
            print("no accounts")
        for i in ids:
            ac = store.accounts.get(i)
            if ac:
                print(f"{i}: {format_amount(ac.balance_minor, ac.currency)}, margin {int(ac.margin * 100)}%")


def main(argv=None):
    p = argparse.ArgumentParser(prog="gateway",
                                description="Safe AgentCash gateway — power-tool + multi-currency billing.")
    p.add_argument("--agentcash", default=None,
                   help="override agentcash binary (default: 'agentcash' or 'npx agentcash@latest')")
    p.add_argument("--store", default="gateway_accounts.json", help="accounts file")
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
    c.add_argument("--price", type=float, default=None, help="expected USD price (budget + billing pre-auth)")
    c.add_argument("--max-budget", type=float, default=None, help="hard USD cap for this call")
    c.add_argument("--account", default=None, help="bill the call to this account")
    c.set_defaults(fn=cmd_call)

    ac = sub.add_parser("account")
    ac.add_argument("sub", choices=["create", "fund", "status"])
    ac.add_argument("id", nargs="?")
    ac.add_argument("--currency", default="USD")
    ac.add_argument("--amount", type=float, default=0.0)
    ac.add_argument("--margin", type=float, default=0.30)
    ac.set_defaults(fn=cmd_account)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
