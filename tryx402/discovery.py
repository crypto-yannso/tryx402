"""Discovery pipeline: public x402 indexes → candidate seed for the catalogue.

Pulls live x402 service listings from public bazaars (Onyx Bazaar JSON, which
mirrors the Coinbase CDP discovery API; gold-402's catalog when reachable),
normalizes them into the gateway's endpoint shape, and emits a *candidate*
seed file. Candidates are NEVER auto-trusted: each entry carries
`verified: false` until a real (cheap) probe call validates price + shape —
the verify-by-real-call flow lives with the hosted registry.

    from tryx402.discovery import refresh_candidates
    result = refresh_candidates("/path/to/seed_candidates.json")
    # {sources: [...], candidates: 137, written: True}

Dedupe key: (origin, path). Price is kept as the DISPLAY string exactly as
published; conversion to USD floats happens only in the ledger, per the
"convert per KEY, not per magnitude" rule.
"""
from __future__ import annotations

import json
import urllib.request

BAZAAR_URL = "https://onyx-actions.onrender.com/bazaar.json"
GOLD402_URL = "https://raw.githubusercontent.com/Haustorium12/gold-402/main/catalog.json"

USER_AGENT = "tryx402-discovery/0.1 (+https://www.tryx402.app)"


def _fetch(url: str, timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _from_onyx(data: dict) -> list[dict]:
    out = []
    for row in data.get("rows") or []:
        resource = row.get("resource")
        if not resource:
            continue
        parts = resource.split("://", 1)[-1].split("/", 1)
        origin = f"https://{parts[0]}"          # published listings are https
        path = "/" + parts[1] if len(parts) > 1 else "/"
        out.append({
            "origin": origin,
            "endpoint": path,
            "price_display": row.get("price"),           # verbatim, e.g. "$0.006000"
            "network": row.get("network"),
            "description": row.get("description") or "",
            "popularity": {"calls_30d": row.get("calls_30d"),
                           "payers_30d": row.get("payers_30d"),
                           "last_called": row.get("last_called")},
            "source": "onyx-bazaar",
            "verified": False,
        })
    return out


def _from_gold402(data) -> list[dict]:
    """gold-402 ships a large hand-curated catalog; tolerate schema drift."""
    out = []
    rows = data if isinstance(data, list) else data.get("entries") or data.get("services") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("url") or row.get("resource") or ""
        if not url.startswith("http"):
            continue
        parts = url.split("://", 1)[-1].split("/", 1)
        out.append({
            "origin": f"{url.split('://')[0]}://{parts[0]}",
            "endpoint": "/" + parts[1] if len(parts) > 1 else "/",
            "price_display": row.get("price"),
            "network": row.get("network"),
            "description": row.get("description") or "",
            "popularity": {},
            "source": "gold-402",
            "verified": False,
            "editorial": row.get("badge") or row.get("note"),
        })
    return out


SOURCES = {
    "onyx-bazaar": (BAZAAR_URL, _from_onyx),
    "gold-402": (GOLD402_URL, _from_gold402),
}


def collect(sources: list[str] | None = None) -> dict:
    """Fetch + normalize + dedupe. Never raises on a dead source; reports it."""
    chosen = sources or list(SOURCES)
    candidates: dict[tuple, dict] = {}
    report = {}
    for name in chosen:
        url, fn = SOURCES[name]
        raw = _fetch(url)
        if raw is None:
            report[name] = {"ok": False, "reason": "fetch failed"}
            continue
        try:
            data = json.loads(raw.decode())
        except json.JSONDecodeError:
            report[name] = {"ok": False, "reason": "invalid JSON"}
            continue
        items = fn(data)
        added = 0
        for it in items:
            k = (it["origin"], it["endpoint"])
            if k not in candidates:            # first source wins; keep provenance
                candidates[k] = it
                added += 1
        report[name] = {"ok": True, "items": len(items), "new_after_dedupe": added}
    ordered = sorted(candidates.values(),
                     key=lambda c: -(c["popularity"].get("calls_30d") or 0))
    return {"sources": report, "candidates": ordered}


def refresh_candidates(out_path: str, sources: list[str] | None = None,
                       top_n: int | None = None) -> dict:
    """Collect and write the candidate seed. `top_n` caps output size."""
    result = collect(sources)
    items = result["candidates"]
    if top_n:
        items = items[:top_n]
    payload = {
        "meta": {"candidate_count": len(items),
                 "all_verified_false": all(not c["verified"] for c in items)},
        "endpoints": items,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    result["written"] = out_path
    result["candidates"] = len(items)
    return result
