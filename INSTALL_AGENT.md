# Agent installation instruction — tryx402

Copy-paste this whole block to ANY coding agent (Hermes, Claude Code, Cursor,
Codex…). The agent does everything else. The human only supplies two things,
marked [USER], and approves nothing else.

---

You are installing **tryx402** (https://github.com/crypto-yannso/tryx402), a
safe gateway over AgentCash/x402 for AI agents: hard budget caps, idempotent
paid calls, cost ledger, per-account API keys, optional Stripe funding.
Follow these steps exactly. Do not improvise alternatives unless a step fails
independently of you; if it does, report the exact error and stop.

## Step 1 — Install

```bash
git clone https://github.com/crypto-yannso/tryx402 ~/.tryx402/repo
cd ~/.tryx402/repo
pip install -e .
```

Requirements check first: Python ≥ 3.9 and either the `agentcash` CLI on PATH
or Node/npx (fallback is `npx agentcash@latest`). Report which one you found.

## Step 2 — Verify offline (no money moves)

ALWAYS set PYTHONPATH for every python command in this instruction:

```bash
PYTHONPATH=~/.tryx402/repo python3 gateway/tests/test_gateway.py
```

Expected final line: `ALL GATEWAY TESTS PASSED`. If not, stop and report.

## Step 3 — Register this agent with its own account

Create the data dir first, then register:

```bash
mkdir -p ~/.tryx402
python3 - <<'EOF'
import os
from gateway.api import Gateway
store_path = os.path.expanduser("~/.tryx402/accounts.json")
gw = Gateway(store_path=store_path)
account_id = os.environ.get("TRYX402_ACCOUNT_ID", "<AGENT-NAME>")
acct, key = gw.create_account(account_id, currency="EUR", margin=0.30, issue_key=True)
print("ACCOUNT:", account_id)
print("API_KEY (shown once):", key)
EOF
```

Replace `<AGENT-NAME>` with a short identifier of yourself (e.g. `hermes-main`).
Save the printed key to `~/.tryx402/key` with permissions 600:

```bash
umask 077 && echo "THE_KEY" > ~/.tryx402/key
```

## Step 4 — Set a spending cap

Write the file `~/.tryx402/budget` containing a single number: the maximum USD
this agent may spend in total, e.g. `2.00`.
Ask the user [USER] for this number if not already given. Default proposal:
**2.00**. Never exceed what the user approved.

## Step 5 — Fund the account (required before any paid call)

Accounts start at zero. Ask the user [USER] how much working credit to load
(default proposal: **1.00 EUR**, always below the step-4 cap). Then fund via
the gateway's own ledger — this is internal bookkeeping, NOT an on-chain
transaction; real USDC is only spent later per call:

```bash
gateway --store ~/.tryx402/accounts.json account fund <AGENT-NAME> --amount 1.00
```

NOTE: the global `--store` flag comes BEFORE the subcommand. Every `gateway`
CLI call in your future usage must repeat it (or export
`TRYX402_STORE=~/.tryx402/accounts.json` if your shell allows).

Verify: balance shows the funded amount in EUR minor units (100 = 1.00 EUR).

## Step 6 — Wire the MCP tools (if your runtime supports MCP)

Add this server so you get `gateway_search`, `gateway_discover`, `gateway_call`,
`gateway_spent` as native tools. Use your runtime's native command when one
exists; otherwise write the JSON block into its MCP config verbatim:

```json
{
  "mcpServers": {
    "tryx402": {
      "command": "python3",
      "args": ["-m", "gateway.mcp_server"],
      "cwd": "~/.tryx402/repo",
      "env": {
        "PYTHONPATH": "~/.tryx402/repo",
        "GATEWAY_MAX_BUDGET_USD": "<the number from step 4>",
        "TRYX402_STORE": "~/.tryx402/accounts.json"
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
gateway --store ~/.tryx402/accounts.json call \
  https://stable-deepline.dev/api/email/validate \
  --body '{"email":"test@example.com"}' --price 0.03 \
  --max-budget 0.05 --account <AGENT-NAME>
```

A JSON response with `"status"` inside proves the full rail works end to end:
budget check → x402 payment → ledger entry → account debited at margin.
If it fails with `InsufficientBalance`, go back to step 5 — you forgot to fund.

## Usage after install

Python (always with PYTHONPATH=~/.tryx402/repo or after `pip install -e .`
from that directory):
```python
from gateway import Gateway
gw = Gateway(max_budget_usd=5.0, store_path="~/.tryx402/accounts.json")
data = gw.call(url, body={...}, price=0.03, account="<AGENT-NAME>")
```

Rules you must always follow afterwards:
1. Never call a paid endpoint without `price` (expected cost) set.
2. Never raise the budget without explicit human approval.
3. Check `gateway_spent` before any batch of calls; stay under the cap.
4. A timed-out paid call must never be blindly retried (double-charge risk).
5. When the funded balance runs low, ask [USER] before re-funding (step 5).

Report when done: Python version, agentcash binary found, test result, account
id created, budget written, funded amount, smoke-test outcome. Do not print the
API key back.
