"""x402-native facade for the hosted service.

Lets external AI agents (AWS AgentCore, Claude, Circle, any x402 client) pay
tryx402 directly with the x402 protocol: HTTP 402 + X-PAYMENT header. This is
what makes wrapped web2 tools (Apify etc.) listable on aggregators like the
Coinbase x402 Bazaar and x402scan.

Design constraints carried over from the gateway's payment lessons:
  - prices come from the SERVER registry, never from the client
  - atomic amounts are exact integers (USDC has 6 decimals), never floats
  - a possibly-paid call is NEVER retried automatically
"""
from __future__ import annotations

from typing import List, Optional

__all__ = ["build_accepts", "FacadeConfigError", "build_402_response"]

# Base mainnet USDC by default; overridable per environment.
DEFAULT_NETWORK = "base"
DEFAULT_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# USDC uses 6 decimals on Base and Solana.
USDC_DECIMALS = 6
DEFAULT_MAX_TIMEOUT_SECONDS = 60
X402_VERSION = 1

# Legacy alias -> CAIP-2 canonical names (facilitators register by CAIP-2)
_NETWORK_ALIASES = {
    "base": "eip155:8453",
    "base-sepolia": "eip155:84532",
    "solana": "solana:5eykt4UsFv8P8NJdTREpYogo7bWf6QEv",  # solana mainnet
    "solana-devnet": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
}


def canonical_network(network: str) -> str:
    """Map a legacy network alias to its CAIP-2 canonical name."""
    return _NETWORK_ALIASES.get(network or "", network or "")


class FacadeConfigError(Exception):
    """Misconfigured facade parameters (missing pay-to, bad price...)."""


def _atomic_units(price_cents: int, decimals: int = USDC_DECIMALS) -> int:
    """Convert cents to atomic token units as an exact integer."""
    return int(price_cents) * (10 ** decimals) // 100


def build_accepts(
    resource_url: str,
    description: str,
    price_cents: int,
    pay_to: str,
    network: str = DEFAULT_NETWORK,
    asset: Optional[str] = None,
    max_timeout_seconds: int = DEFAULT_MAX_TIMEOUT_SECONDS,
) -> List[dict]:
    """Build the x402 `accepts` payment-requirements payload."""
    if not pay_to or not pay_to.strip():
        raise FacadeConfigError("pay_to is required")
    if price_cents is None or int(price_cents) <= 0:
        raise FacadeConfigError("price_cents must be positive")

    entry = {
        "scheme": "exact",
        "network": canonical_network(network or DEFAULT_NETWORK),
        # v1 wire format: amounts are strings (SDK pydantic requirement)
        "maxAmountRequired": str(_atomic_units(int(price_cents))),
        "asset": asset or DEFAULT_ASSET,
        "payTo": pay_to.strip(),
        "resource": resource_url,
        "description": description,
        "maxTimeoutSeconds": int(max_timeout_seconds),
        "mimeType": "",
    }
    if network and network.startswith("solana"):
        # Solana assets are referenced by mint address, feePayer required by spec
        entry["feePayer"] = pay_to.strip()
    return [entry]


def build_accepts_for_tool(resource_url: str, origin: str,
                           price_cents: int, pay_to: str) -> list:
    """Build the x402 `accepts` payload for one catalogue tool.

    The resource is the shared /v1/x402/call endpoint; the origin being sold
    is carried in the description so facilitators and agents can attribute
    the payment.
    """
    return build_accepts(
        resource_url=resource_url,
        description=f"tryx402 proxy for {origin}",
        price_cents=price_cents,
        pay_to=pay_to,
    )


def build_402_response(
    resource_url: str,
    description: str,
    price_cents: int,
    pay_to: str,
    error: Optional[str] = None,
) -> dict:
    """Build the full JSON body returned with an HTTP 402."""
    body = {
        "x402Version": X402_VERSION,
        "error": error or "X-PAYMENT header is required",
        "accepts": build_accepts(
            resource_url=resource_url,
            description=description,
            price_cents=price_cents,
            pay_to=pay_to,
        ),
    }
    return body
