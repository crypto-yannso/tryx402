# Agent installation instruction — tryx402

Copy-paste this whole block to ANY coding agent (Hermes, Claude Code, Cursor,
Codex…). The agent does everything else. The human only supplies two things,
marked [USER], and approves nothing else.

---

You are installing **tryx402** (https://github.com/crypto-yannso/tryx402), a
safe gateway over AgentCash/x402 for AI agents: hard budget caps, idempotent
paid calls, cost ledger. Accounts, balances and pricing live on the hosted
service (https://www.tryx402.app) — this SDK talks to it with an API key.
Follow these steps exactly. Do not improvise alternatives unless a step fails
independently of you; if it does, report the exact error and stop.

## Step 1 — Install

```bash
pip install tryx402
```

(or from source: `git clone https://github.com/crypto-yannso/tryx402 ~/.tryx402/repo && cd ~/.tryx402/repo && pip install -e .`)

Requirements check first: Python ≥ 3.9 and either the `agentcash` CLI on PATH
or Node/npx (fallback is `npx agentcash@latest`). Report which one you found.

## Step 2 — Verify offline (no money moves)

```bash
python3 gateway/tests/test_gateway.py
```

Expected final line: `ALL GATEWAY TESTS PASSED`. If not, stop and report.

## Step 3 — Get an API key [USER]

The human signs up at https://www.tryx402.app and creates an account for the
agent (e.g. named `hermes-main`). They receive an API key starting with `gw_`,
shown once. Save it:

```bash
umask 077 && echo "gw_THE_KEY" > ~/.tryx402/key
export TRYX402_API_KEY=$(cat ~/.tryx402/key)
```

## Step 4 — Set a spending cap

Write the file `~/.tryx402/budget` containing a single number: the maximum USD
this agent may spend in total, e.g. `2.00`.
Ask the user [USER] for this number if not already given. Default proposal:
**2.00**. Never exceed what the user approved.

## Step 5 — Fund the account [USER]

Accounts start at zero. The human funds their account at
https://www.tryx402.app (card payment via Stripe). Ask them to confirm when
the balance shows the working credit they want to allocate (default proposal:
**1.00 EUR**). Check it:

```bash
gateway quote 0.01    # any positive number; proves auth + returns your price
```

## Step 6 — Wire the MCP tools (if your runtime supports MCP)

Add this server so you get `gateway_search`, `gateway_discover`, `gateway_call`,
`gateway_spent` as native tools:

```json
{
  "mcpServers": {
    "tryx402": {
      "command": "python3",
      "args": ["-m", "gateway.mcp_server"],
      "env": {
        "GATEWAY_MAX_BUDGET_USD": "<the number from step 4>",
        "TRYX402_API_KEY": "<the key from step 3>"
      }
    }
  }
}
```

On Hermes specifically: do NOT use `hermes mcp add --args` (it swallows `--env`);
edit the config file directly with the JSON above. If your runtime has no MCP:
skip this step; use the Python or CLI form below.

## Step 7 — Smoke test (spends ~$0.03 real USDC)

Ask the user [USER] for confirmation before running. Then:

```bash
gateway call https://stable-deepline.dev/api/email/validate \
  --body '{"email":"test@example.com"}' --price 0.03 \
  --max-budget 0.05
```

A JSON response with `"status"` inside proves the full rail works end to end:
budget check → x402 payment → ledger entry.
If it fails with a budget error, re-check step 4.

## Usage after install

```python
from gateway import Gateway
gw = Gateway(max_budget_usd=5.0)   # api_key read from TRYX402_API_KEY
data = gw.call(url, body={...}, price=0.03)
print(gw.quote(0.03))              # server-side price for your account
```

Rules you must always follow afterwards:
1. Never call a paid endpoint without `price` (expected cost) set.
2. Never raise the budget without explicit human approval.
3. Check `gateway_spent` before any batch of calls; stay under the cap.
4. A timed-out paid call must never be blindly retried (double-charge risk).
5. When the funded balance runs low, ask [USER] before re-funding (step 5).

Report when done: Python version, agentcash binary found, test result, budget
written, smoke-test outcome. Do not print the API key back.
