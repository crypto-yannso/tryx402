# Changelog

## 0.4.0 — x402 facade + trust layer (server)

Everything a third-party x402 agent needs to pay tryx402-wrapped endpoints
directly on the x402 rail, plus the auditability to prove it afterwards.

- **Public x402 facade**: spec-conformant 402 paywall (`/v1/x402/call`),
  facilitator verify/settle, exactly-no-retry upstream discipline,
  reconciliation flag on post-payment failures.
- **Crawlable catalog** (`/v1/x402/tools`): every wrapped origin with its
  server-set price as an x402 `accepts` entry. v1 wire format: amounts are
  strings (official SDK pydantic requirement — caught by a live client).
- **Syndication endpoints** (`/v1/x402/listing`, `/v1/x402/bazaar.json`):
  generic x402 resources document + Bazaar-style flat feed so aggregators
  (Onyx Bazaar, gold-402, x402scan) index tryx402 without bespoke
  integration. Private origins never exported; exported prices are exactly
  what the facade charges.
- **Signed Ed25519 receipts** on paid responses (`X-RECEIPT`), validated
  against RFC 8032 test vectors, verifiable offline.
- **Replay protection**: duplicate payments answered with 409
  `duplicate_payment` (bounded in-process cache).
- **Session auth on the hosted API** (BREAKING for 0.3.x SDK clients):
  `/v1/auth/session` mints X-Customer-ID + X-Session-Token; server-side
  pricing via PriceRegistry seeded from the tools DB; atomic SQLite debit;
  idempotent Stripe webhook; automatic refund on provider failure.
- Fly.io wiring: `TRYX402_TOOLS_DB_PATH` on a persistent volume; sessions
  survive scale-to-zero.

## 0.3.0 — competitive-parity release (branch `feat/competitive-parity`)

Patterns adopted from open-source peers (all MIT/Apache; no code copied, patterns reimplemented):

- **Plan estimation before payment** (`tryx402/planner.py`, from agentpay's
  `estimate_plan`): price a multi-call workflow for free, flag unknown prices
  as `-1.0` (never guessed), report over-budget step indices.
  Exposed as `Gateway.plan()` and MCP tool `gateway_plan`.
- **Signed receipts** (`tryx402/receipts.py`, TrustBench pattern): pure-Python
  Ed25519 validated against RFC 8032 test vectors; offline-verifiable receipts
  binding {origin, endpoint, amount, tx_hash, ts, idem}. Key via
  `TRYX402_RECEIPT_KEY`. Exposed as `Gateway.receipt()` / MCP `gateway_receipt`.
- **Session tokens + circuit breaker** (`tryx402/sessions.py`, a2a-x402 #60 +
  agentpay-mcp): HMAC-signed per-session spend caps with TTL; per-origin
  breaker with half-open cooldown probe. `Gateway.session(cap_usd=...)`.
- **Transparent 402→pay→retry transport** (`tryx402/http_transport.py`,
  x402-anthropic pattern generalized to any payer): httpx transport + an
  httpx-free `pay_and_retry()`; exactly one retry, double-402 raises,
  post-payment failure raises `PaidCallTimeout`; budget checked pre-payment.
- **Discovery pipeline** (`tryx402/discovery.py`): public bazaars → candidate
  seed with `verified: false`; dedupe on (origin, path); live-tested against
  Onyx Bazaar (100 endpoints incl. Chainlink, Tavily).

All additions are backward-compatible: existing `Gateway.call/search/discover/
quote/spent_usd/ledger` behavior unchanged; existing test suite passes.

## 0.2.0 — open-core split

Server-side pricing removed from the SDK; margin/FX live on the hosted service.

## 0.1.x — initial public SDK

Budget-capped safe caller over the AgentCash CLI, idempotency cache, cost ledger.
