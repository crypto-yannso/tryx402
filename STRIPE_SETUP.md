# Wiring real Stripe to the gateway (fiat face)

**You do the auth.** I never handle your Stripe secret keys, and neither the
Stripe CLI nor a Stripe MCP is available in this environment — so the commands
below are yours to run. The code that consumes Stripe (`gateway/stripe_integration.py`)
is already written and tested; it just needs your keys.

## Option A — Stripe CLI

```bash
# 1. Install (macOS)
brew install stripe/stripe-cli/stripe

# 2. Authenticate (opens a browser; you approve) — I cannot do this step
stripe login

# 3. Run the gateway's webhook receiver (in terminal 1)
STRIPE_WEBHOOK_SECRET=whsec_xxx \
  python3 -m gateway.stripe_integration --serve --store gateway_accounts.json --port 4242

# 4. Forward Stripe events to it (terminal 2). This prints the whsec_... to use above.
stripe listen --forward-to localhost:4242

# 5. Fire a test payment (terminal 3) — credits the 'acme' account by amount_total
stripe trigger checkout.session.completed \
  --add checkout_session:metadata.account_id=acme
```

The webhook is HMAC-verified in `verify_webhook`; on `checkout.session.completed`,
`handle_event` credits the account named in `metadata.account_id` by `amount_total`.

## Option B — Stripe MCP (official)

Stripe publishes an official MCP server (`@stripe/mcp`). Add it in an
**interactive** Claude session (this one can't run the OAuth/key step):

```bash
claude mcp add stripe -- npx -y @stripe/mcp --tools=all --api-key=sk_test_xxx
```

Then create Checkout sessions through the Stripe MCP tools using the body from
`gateway.stripe_integration.checkout_params(account_id, amount_minor, currency, ...)`.
(Confirm the exact flags against Stripe's current MCP docs.)

## What the code already does (tested)

| Piece | Role |
|---|---|
| `verify_webhook(payload, sig, secret)` | HMAC-verifies the Stripe signature (stdlib), rejects forgeries |
| `handle_event(event, store)` | on `checkout.session.completed` → `store.credit_minor(account_id, amount_total)` |
| `checkout_params(...)` | the Checkout Session body to create (account_id in metadata) |
| `run_webhook_server(store, secret, port)` | minimal receiver for `stripe listen` |

## Test vs live

Use `sk_test_…` / test webhooks until the flow is green, then swap to live keys.
Never commit keys — read them from the environment (as `--serve` does with
`STRIPE_WEBHOOK_SECRET`).
