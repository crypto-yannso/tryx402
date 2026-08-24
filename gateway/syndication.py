"""Syndication export: registry -> aggregator listing documents.

One source of truth (the PriceRegistry), N outputs: a generic x402
resources document for discovery crawls and a Bazaar-style feed with the
fields aggregators require to index a paid endpoint.

Private origins are never exported. Prices stay server-authoritative:
the exported values are exactly what /v1/x402/call will charge.
"""
from __future__ import annotations

import json
import os
from typing import List

from .registry import PriceRegistry
from .x402_facade import DEFAULT_ASSET, DEFAULT_NETWORK, USDC_DECIMALS

__all__ = ["export_listing", "write_listing", "bazaar_feed",
           "SyndicationConfigError"]


class SyndicationConfigError(Exception):
    """Missing settlement address or bad base URL."""


def _atomic_units(price_cents: int) -> int:
    return int(price_cents) * (10 ** USDC_DECIMALS) // 100


def export_listing(registry: PriceRegistry, base_url: str,
                   pay_to: str, network: str = DEFAULT_NETWORK,
                   asset: str = DEFAULT_ASSET) -> dict:
    """Build the generic x402 resources listing document."""
    if not pay_to or not pay_to.strip():
        raise SyndicationConfigError("pay_to is required for syndication")
    base = (base_url or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise SyndicationConfigError(f"invalid base_url: {base_url!r}")

    resources: List[dict] = []
    for origin, price_cents, allow_private in sorted(registry.items()):
        if allow_private:
            continue
        resources.append({
            "origin": origin,
            "resource": f"{base}/v1/x402/call",
            "scheme": "exact",
            "network": network,
            "asset": asset,
            "maxAmountRequired": _atomic_units(price_cents),
            "priceDisplay": f"${price_cents / 100:.2f}",
            "payTo": pay_to.strip(),
        })
    return {"x402Version": 1, "resources": resources}


def write_listing(registry: PriceRegistry, out_path, base_url: str =
                  "https://tryx402.fly.dev", pay_to: str = "") -> int:
    """Write the listing document to disk; returns number of resources."""
    doc = export_listing(registry, base_url=base_url,
                         pay_to=pay_to or os.environ.get(
                             "TRYX402_PAY_TO_ADDRESS", ""))
    parent = os.path.dirname(os.path.abspath(str(out_path)))
    os.makedirs(parent, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
        fh.write("\n")
    return len(doc["resources"])


def bazaar_feed(registry: PriceRegistry, base_url: str, pay_to: str,
                network: str = DEFAULT_NETWORK,
                asset: str = DEFAULT_ASSET) -> List[dict]:
    """Flat per-endpoint items in aggregator feed style."""
    doc = export_listing(registry, base_url=base_url, pay_to=pay_to,
                         network=network, asset=asset)
    feed = []
    for r in doc["resources"]:
        feed.append({
            "id": f"tryx402:{r['origin']}",
            "endpoint": f"{r['origin']}/",
            "method": "POST",
            "price": r["maxAmountRequired"],
            "priceDisplay": r["priceDisplay"],
            "scheme": r["scheme"],
            "network": r["network"],
            "asset": r["asset"],
            "payTo": r["payTo"],
            "resource": r["resource"],
        })
    return feed
