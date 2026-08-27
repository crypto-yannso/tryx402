"""tryx402 — safe, provider-agnostic x402 payment governance for AI agents.

Adds what AgentCash itself lacks and we learned we need the hard way:
  * hard budget caps        (stop before overspending)
  * idempotency             (never pay twice for the same call — the $0.83 lesson)
  * cost ledger             (USD/EUR spent, grouped by origin / account)
  * plan estimation         (price a workflow before spending)
  * signed receipts         (Ed25519 offline-verifiable proof)
  * session tokens + breaker (per-task caps with half-open cooldown)
  * transparent 402→pay→retry (native httpx transport)
  * discovery pipeline      (live x402 bazaar indexing)

MIT licensed. Built in France.
"""

__version__ = "0.4.1"

from ._telemetry import _ping  # noqa: F401  — best-effort, never raises

from .api import Gateway
from .client import AgentCashError, BudgetExceeded, SafeClient
from .ledger import Ledger

__all__ = ["Gateway", "SafeClient", "Ledger", "BudgetExceeded", "AgentCashError"]
