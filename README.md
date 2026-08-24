# tryx402 — the OpenRouter for agent payments

<!-- mcp-name: io.github.crypto-yannso/tryx402 -->

A safe, **provider-agnostic** wrapper over AgentCash / x402. Not limited to
prospecting — it drives *any* endpoint, and adds what AgentCash itself lacks and
we learned we need the hard way:

- **hard budget caps** — stop before overspending
- **idempotency** — never pay twice for the same call (the $0.83 double-charge lesson)
- **cost ledger** — USD spent, reconciled, grouped by origin / account
- **multi-currency billing + margin** — end users pay in THEIR currency (EUR, USD, GBP, JPY…), never touch the wallet

## Two faces, one engine

| Face | Who | Module |
|---|---|---|
| **Power-tool** (option 3) | you / an agent drive the whole catalogue safely | `gateway.cli`, `gateway.client` |
| **Fiat gateway** (option 2) | end users pay in their own currency, you resell x402 with a margin | `gateway.accounts` (Stripe stubbed) |

Prospecting (`../prospect_relay`) is just **one application** on top of this core.

## Embed it (for agents)

**Python — one import:**

```python
from gateway import Gateway
gw = Gateway(max_budget_usd=1.0)               # safe by default
data = gw.call("https://stable-deepline.dev/api/email/validate",
               body={"email": "x@y.com"}, price=0.03)
print(gw.spent_usd)                            # 0.03
```

**Terminal — one command** (like `agentcash`), after `pip install -e .`:

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

Open-source SDK. The hosted backend (billing API, verified-tools catalogue, provider portal) is closed — see https://www.tryx402.app

```
gateway/
├── client.py    # SafeClient: any endpoint + budget + idempotency + timeout + ledger
├── ledger.py    # cost events, USD/EUR totals, by-origin / by-account
├── catalog.py   # search(intent) / discover(origin) over the AgentCash catalogue
├── accounts.py  # per-currency balances (integer minor units), margin, authorize/charge — Stripe stub
├── cli.py       # power-tool CLI (drives both faces)
└── tests/       # offline: budget, idempotency, EUR billing (no spend)
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

Run it as a **fiat gateway** — the caller pays in their own currency, never sees the wallet:

```bash
python3 -m gateway.cli account create acme --currency GBP --margin 0.30
python3 -m gateway.cli account fund acme --amount 20            # 20 GBP — Stripe stub
python3 -m gateway.cli call https://stable-deepline.dev/api/email/validate \
  --body '{"email":"foo@bar.com"}' --price 0.03 --account acme
#  -> billed 0.04 GBP to acme (margin 30%, balance 19.96 GBP)
```

Provider cost stays in USD (the rail settles in USDC); each account is billed in
its own currency via `FxRates` (a stub table today — wire a live FX feed in prod).
Non-2-decimal currencies (JPY, KWD…) are handled.

## What's real vs stubbed

- **Real:** the safe caller, budget cap, idempotency, cost ledger, multi-currency/margin math (integer minor units), catalogue search/discover.
- **Stubbed:** funding an account (`fund_eur`) stands in for the **Stripe webhook**. Wire real Stripe → call `fund_eur` on `checkout.session.completed`. Everything downstream already works.
- **Deliberately off:** auto-retry of paid calls (default `max_retries=0`) — retrying a possibly-paid call is what double-charged us; opt in only if the server honors the `Idempotency-Key` header.
