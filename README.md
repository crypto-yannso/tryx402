# tryx402 — the OpenRouter for agent payments

<!-- mcp-name: io.github.crypto-yannso/tryx402 -->

**MIT licensed. Built in France. The first open-source payment governance layer for AI agents.**

A safe, **provider-agnostic** wrapper over AgentCash / x402. Not limited to
prospecting — it drives *any* endpoint, and adds what AgentCash itself lacks and
we learned we need the hard way:

- **hard budget caps** — stop before overspending
- **idempotency** — never pay twice for the same call (the $0.83 double-charge lesson)
- **cost ledger** — USD spent, reconciled, grouped by origin / account
- **plan estimation** — price a multi-call workflow before spending a single cent
- **signed receipts** — Ed25519 offline-verifiable proof of every payment
- **session tokens + circuit breaker** — per-task spend caps with half-open cooldown
- **transparent 402→pay→retry** — native httpx transport, exactly one retry, no loops
- **discovery pipeline** — live x402 bazaar indexing (Onyx Bazaar, gold-402)

## Open source, no fear

This repo is the **open-source SDK**: the safe caller, budget guard, idempotency,
ledger, CLI and MCP server. MIT licensed.

We don't hide features behind paywalls. We don't split the SDK into "core" and "pro".
Everything is here. Use it, fork it, build on it.

The **hosted service** (https://www.tryx402.app) is the commercial engine:
multi-currency fiat billing, margins, FX, Stripe funding, the verified-tools
catalogue and the provider portal. Pricing is decided **server-side** via
`/v1/quote` — a client-side margin is a margin nobody pays.

**Revenue model**: commission on every pay-per-call transaction routed through
the hosted service. The SDK is free. The service takes a cut. That's it.

## Positionnement

tryx402 est le **premier SDK open source de gouvernance de paiements pour agents IA** 
dans l'écosystème x402. Conçu en France, distribué sous MIT, adopté par la communauté.

Face aux agrégateurs propriétaires (treg.to et consorts), tryx402 défend une position 
différente : pas de lock-in, pas de licence restrictive, pas de "commercial use only". 
Le code est libre. La confiance vient du service, pas de la licence.

| Face | Who | Where |\n|---|---|---|\n| **Power-tool** | you / an agent drive paid endpoints safely | this SDK (`gateway.cli`, `gateway.client`) |\n| **Fiat gateway** | end users pay in their currency, you resell x402 with margin | hosted service (closed) |

## Embed it (for agents)

**Python — one import:**

```python
from gateway import Gateway
gw = Gateway(max_budget_usd=1.0)               # safe by default
data = gw.call("https://stable-deepline.dev/api/email/validate",
               body={"email": "x@y.com"}, price=0.03)
print(gw.spent_usd)                            # 0.03
```

**Terminal — one command** (like `agentcash`), after `pip install tryx402`:

```bash
gateway call https://stable-deepline.dev/api/email/validate \
  --body '{"email":"x@y.com"}' --price 0.03 --max-budget 0.10
```

**MCP — two modes (pick the one you need):**

### SDK MCP (pip install) — 6 tools, safe caller only

```json
{
  "command": "python3",
  "args": ["-m", "tryx402.mcp_server"],
  "env": { "TRYX402_MAX_BUDGET_USD": "1.00" }
}
```

6 tools: `gateway_search`, `gateway_discover`, `gateway_call`, `gateway_spent`,
`gateway_plan`, `gateway_receipt`. Works directly with your x402/AgentCash
crypto wallet — no hosted account needed.

> **You will NOT see** `gateway_check_balance`, `gateway_recharge`,
> `gateway_lookup`, or `gateway_proxy_call` from `pip install tryx402`.
> This is deliberate, not a bug. These tools manage your **hosted account
> credits** (funded via Stripe, separate from your crypto wallet). The SDK
> MCP is a safe x402 caller; the hosted gateway handles the fiat credit
> layer. For the full 10-tool MCP, see Option B below or connect to
> `https://www.tryx402.app/api/mcp`.

### Gateway MCP (clone repo) — 10 tools, hosted account management

```json
{
  "mcpServers": {
    "tryx402": {
      "command": "python3",
      "args": ["-m", "gateway.mcp_server"],
      "env": {
        "GATEWAY_MAX_BUDGET_USD": "1.00"
      }
    }
  }
}
```

All 6 SDK tools **plus** `gateway_session`, `gateway_check_balance`,
`gateway_recharge`, `gateway_proxy_call`. No API key needed — anonymous
session auth throughout. `check_balance` / `recharge` manage your
**hosted account credits** (funded via Stripe in EUR/USD — not crypto).

## Layout

```
gateway/
├── client.py    # SafeClient: any endpoint + budget + idempotency + timeout + ledger
├── ledger.py    # cost events, USD/EUR totals, by-origin
├── catalog.py   # search(intent) / discover(origin) over the AgentCash catalogue
├── api.py       # Gateway facade: one-import embed + quote() for server pricing
├── cli.py       # search / discover / call / quote
└── tests/       # offline: budget, idempotency, on_paid hook, quote client
```

## Use

Discover (free — no payment):

```bash
python3 -m gateway.cli search "find work email"
python3 -m gateway.cli discover https://stable-deepline.dev
```

Call any endpoint safely (spends USDC — budget-capped, idempotent):

```bash
python3 -m gateway.cli call https://stable-deepline.dev/api/email/validate \
  --body '{"email":"foo@bar.com"}' --price 0.03 --max-budget 0.10
```

Ask the hosted service what a call costs your account (server-side FX + margin):

```bash
export TRYX402_API_KEY="gw_..."        # from https://www.tryx402.app
gateway quote 0.04
#  -> {"account":"acme","currency":"EUR","charge_minor":5,"charge":"0.05 EUR"}
```

## What's real vs hosted

- **Real (this SDK):** the safe caller, budget cap, idempotency, cost ledger, catalogue search/discover.
- **Hosted:** accounts, balances, fiat funding (Stripe), FX conversion, margins — all decided by the server; the SDK only asks with `quote()`.
- **Deliberately off:** auto-retry of paid calls (default `max_retries=0`) — retrying a possibly-paid call is what double-charged us; opt in only if the server honors the `Idempotency-Key` header.

## Telemetry (optional)

tryx402 sends **one anonymous ping on first import** to help us understand usage:
install-id (random, persisted locally), package version, Python version, platform.

No PII. No tracking across sites. No personal data.

**Disable it anytime:**
```bash
export TRYX402_NO_TELEMETRY=1
```

Or set an empty URL to disable:
```bash
export TRYX402_TELEMETRY_URL=""
```
