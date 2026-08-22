"""Real Stripe funding for the fiat gateway (replaces the `fund` stub).

We never touch secret keys in code you didn't write: this module only
*verifies* the webhook Stripe sends and credits the matching account. Creating
Checkout sessions and auth stay on your side (Stripe CLI or the Stripe MCP).

Flow:
  1. you create a Checkout session (params from `checkout_params`) — with the
     account_id in metadata and the amount in the account's currency;
  2. the customer pays on Stripe's hosted page (you never see card data);
  3. Stripe POSTs `checkout.session.completed` to your webhook;
  4. `verify_webhook` HMAC-checks it, `handle_event` credits the account.

Signature verification is Stripe's scheme, done with stdlib hmac/hashlib — no SDK.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from .accounts import AccountStore, FxRates


class WebhookError(Exception):
    pass


def verify_webhook(payload, sig_header: str, signing_secret: str, tolerance: int = 300) -> dict:
    """Verify a Stripe webhook signature and return the parsed event."""
    if isinstance(payload, str):
        payload = payload.encode()
    parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        raise WebhookError("malformed Stripe-Signature header")
    signed = t.encode() + b"." + payload
    expected = hmac.new(signing_secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, v1):
        raise WebhookError("signature mismatch")
    if tolerance and abs(time.time() - int(t)) > tolerance:
        raise WebhookError("timestamp outside tolerance")
    return json.loads(payload)


def handle_event(event: dict, store: AccountStore, save: bool = True,
                 rates: "FxRates | None" = None, seen_ids: set | None = None):
    """On checkout.session.completed, credit the account named in metadata.

    Idempotent when `seen_ids` is given (a set of Stripe event ids): a replayed
    event is skipped, never double-credited. Currency-safe via `rates`
    (see AccountStore.credit_minor).
    """
    if event.get("type") != "checkout.session.completed":
        return None
    eid = event.get("id") or ""
    if seen_ids is not None and eid in seen_ids:
        return None                      # replayed delivery — already credited
    sess = event["data"]["object"]
    account_id = (sess.get("metadata") or {}).get("account_id") or sess.get("client_reference_id")
    amount = sess.get("amount_total")        # integer minor units, in `currency`
    currency = (sess.get("currency") or "").upper() or None
    if not account_id or amount is None:
        raise WebhookError("event missing account_id or amount_total")
    acct = store.credit_minor(account_id, amount, currency, rates=rates)
    if save:
        store.save()
    if seen_ids is not None and eid:
        seen_ids.add(eid)
        _save_seen(seen_ids, getattr(store, "seen_path", None))
    return acct


def _save_seen(seen_ids: set, path: str | None):
    """Persist processed event ids next to the store so restarts stay idempotent."""
    import json as _json
    import os as _os
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            known = set(_json.load(f))
    except (OSError, ValueError):
        known = set()
    known |= seen_ids
    _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(sorted(known)[-10_000:], f)   # cap growth


def checkout_params(account_id, amount_minor, currency, success_url, cancel_url,
                    product_name="Gateway credit") -> dict:
    """The Checkout Session body to create (via Stripe CLI / MCP / API)."""
    return {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": account_id,
        "metadata": {"account_id": account_id},
        "line_items": [{
            "price_data": {
                "currency": currency.lower(),
                "product_data": {"name": product_name},
                "unit_amount": int(amount_minor),
            },
            "quantity": 1,
        }],
    }


def run_webhook_server(store_path, signing_secret, port=4242, rates: FxRates | None = None):
    """Minimal stdlib webhook receiver — good for `stripe listen --forward-to`.

    `store_path` lives under ~/.agentcash-gateway/ by default (persistent);
    processed event ids are persisted alongside it so a restart cannot
    double-credit a replayed delivery.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen_path = store_path.rsplit(".", 1)[0] + ".seen.json"
    try:
        with open(seen_path, encoding="utf-8") as f:
            seen_ids = set(json.load(f))
    except (OSError, ValueError):
        seen_ids = set()
    store = AccountStore.load(store_path)
    store.seen_path = seen_path

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = self.rfile.read(length)
            sig = self.headers.get("Stripe-Signature", "")
            try:
                event = verify_webhook(payload, sig, signing_secret)
                acct = handle_event(event, store, rates=rates, seen_ids=seen_ids)
                body = json.dumps({"received": True,
                                   "credited": acct.id if acct else None}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:  # noqa: BLE001
                self.send_response(400)
                self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, *a):
            pass

    print(f"gateway Stripe webhook listening on :{port} -> {store_path}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main(argv=None):
    import argparse
    import os

    p = argparse.ArgumentParser(prog="gateway.stripe_integration")
    p.add_argument("--serve", action="store_true", help="run the webhook receiver")
    p.add_argument("--store", default=None,
                   help="accounts JSON (default: ~/.agentcash-gateway/accounts.json)")
    p.add_argument("--port", type=int, default=4242)
    a = p.parse_args(argv)
    if a.serve:
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
        if not secret:
            raise SystemExit("set STRIPE_WEBHOOK_SECRET (printed by `stripe listen`)")
        default_store = os.path.join(os.path.expanduser("~"), ".agentcash-gateway",
                                     "accounts.json")
        run_webhook_server(a.store or default_store, secret, a.port)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
