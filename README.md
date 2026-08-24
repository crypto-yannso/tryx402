# tryx402 — the OpenRouter for agent payments

<!-- mcp-name: io.github.crypto-yannso/tryx402 -->

A safe, **provider-agnostic** wrapper over AgentCash / x402. Not limited to
prospecting — it drives *any* endpoint, and adds what AgentCash itself lacks and
we learned we need the hard way:

- **hard budget caps** — stop before overspending
- **idempotency** — never pay twice for the same call (the $0.83 double-charge lesson)
- **cost ledger** — USD spent, reconciled, grouped by origin / account

## Open core, closed engine

This repo is the **open-source SDK**: the safe caller, budget guard, idempotency,
ledger, CLI and MCP server. MIT licensed.

The **hosted service** (https://www.tryx402.app) is the commercial engine:
multi-currency fiat billing, margins, FX, Stripe funding, the verified-tools
catalogue and the provider portal. Pricing is decided **server-side** via
`/v1/quote` — a client-side margin is a margin nobody pays.

| Face | Who | Where |
|---|---|---|
| **Power-tool** | you / an agent drive paid endpoints safely | this SDK (`gateway.cli`, `gateway.client`) |
| **Fiat gateway** | end users pay in their currency, you resell x402 with margin | hosted service (closed) |

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

**MCP — the agent-native form** (like AgentCash's own MCP, zero dependencies):

```json
{
  "command": "python3",
  "args": ["-m", "gateway.mcp_server"],
  "env": { "GATEWAY_MAX_BUDGET_USD": "1.00" }
}
```

The agent gets four tools — `gateway_search`, `gateway_discover`, `gateway_call`
(budget-capped, idempotent), `gateway_spent` — sharing one session-wide budget
and cost ledger across every call.

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
