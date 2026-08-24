"""tryx402 — a safe, provider-agnostic wrapper over AgentCash / x402.

It adds the things AgentCash itself does NOT provide, and that we learned the
hard way we need in production:

  * hard budget caps        (stop before overspending)
  * idempotency             (never pay twice for the same call — the $0.83 lesson)
  * a reconciled cost ledger (USD spent, per origin / per account)
  * multi-currency billing  (end users pay in their own currency, never the wallet)

Easy to embed for agents:
    from gateway import Gateway
    gw = Gateway(max_budget_usd=1.0)
    gw.call(url, body={...}, price=0.03)

…or a single terminal command (like `agentcash`) once installed:
    gateway call <url> --body '{...}' --price 0.03 --max-budget 0.10

Prospecting (../prospect_relay) is just one application on top of this core.
"""

__version__ = "0.2.0"

from .api import Gateway
from .client import AgentCashError, BudgetExceeded, SafeClient
from .ledger import Ledger

__all__ = ["Gateway", "SafeClient", "Ledger", "BudgetExceeded", "AgentCashError"]
