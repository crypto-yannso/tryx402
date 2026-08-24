"""Apify provider — wrap Apify actor runs as pay-per-call endpoints.

Design (skill: x402-gateway-integration):
- Apify does NOT speak x402. We are the merchant: the client pays tryx402
  (credits/EUR via the normal billing path), we pay Apify in platform credits
  via the Apify API token. Margin = price_usd minus real Apify compute cost.
- Transport is run-sync-get-dataset-items: one HTTP call, blocking, up to
  APIFY_RUN_SYNC_TIMEOUT_S. No retry on failure: a run that consumed
  compute must never be re-fired automatically (double-charge rule).
- Cost accounting: after each run we read usage_total_usd from the run
  record (/v2/runs/{id}, field usage.totalUsd). -1.0 means unknown and is
  flagged for reconciliation, never faked.
- Margin protection: client-controlled inputs are CLAMPED before the run
  (maxCrawlPages, maxRequestsPerCrawl, maxResults, maxPagesPerCrawl...).
  Without this a single call could cost dollars of compute against a
  $0.05 price. See clamp_run_input.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

APIFY_API = "https://api.apify.com/v2"
DEFAULT_TIMEOUT_S = 300          # run-sync max wait
MAX_ITEMS_DEFAULT = 100
# Mémoire par défaut des runs. Le défaut Apify (8192 MB) coûte 8x plus cher
# en compute units ; 1024 MB suffit pour la plupart des crawlers simple-page.
# Un client peut demander PLUS via memory_mbytes, jamais moins que ce plancher.
DEFAULT_MEMORY_MBYTES = 1024
MIN_MEMORY_MBYTES = 128

# --- garde-fous marge -----------------------------------------------------
# Plafonds appliqués au run_input du client, quel que soit ce qu'il envoie.
# Clés reconnues par la plupart des crawlers Apify.
INPUT_CEILINGS: Dict[str, int] = {
    "maxCrawlPages": 5,
    "maxRequestsPerCrawl": 5,
    "maxPagesPerCrawl": 5,
    "maxResults": 50,
    "maxItems": 50,
    "resultsLimit": 50,
    "maxReviews": 20,
    "maxPlaces": 10,
    "maxRequests": 5,
}
# Plafond dur du nombre d'URLs de départ acceptées
MAX_START_URLS = 5
# Prix public Apify par compute unit (1 CU = 1 GB de RAM pendant 1 heure)
APIFY_CU_PRICE_USD = 0.4


class ApifyError(RuntimeError):
    pass


def get_token(explicit: Optional[str] = None) -> str:
    tok = explicit or os.environ.get("APIFY_TOKEN", "")
    if not tok:
        raise ApifyError("APIFY_TOKEN manquant (env ou paramètre)")
    return tok


def _request(method: str, path: str, *, token: str,
             params: Optional[Dict[str, Any]] = None,
             body: Optional[Dict[str, Any]] = None,
             timeout: int = 30) -> Any:
    q = {"token": token}
    if params:
        for k, v in params.items():
            if v is not None:
                q[str(k)] = str(v)
    url = f"{APIFY_API}{path}?{urllib.parse.urlencode(q)}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise ApifyError(f"Apify HTTP {e.code} sur {path}: {detail}") from e
    except Exception as e:
        raise ApifyError(f"Apify call failed: {type(e).__name__}: {e}") from e


# --- garde-fous -----------------------------------------------------------

def clamp_run_input(run_input: Optional[Dict[str, Any]]) -> tuple:
    """Force les plafonds de coût dans le run_input. Retourne (input_clampé,
    liste des clés modifiées). Le client ne peut PAS dépasser ces plafonds."""
    if not isinstance(run_input, dict):
        return {}, []
    clamped = dict(run_input)
    touched = []

    # startUrls / start_urls : nombre plafonné
    for key in ("startUrls", "start_urls", "urls"):
        v = clamped.get(key)
        if isinstance(v, list):
            n0, n1 = len(v), min(len(v), MAX_START_URLS)
            # éléments dicts {'url': ...} ou strings : on coupe pareil
            clamped[key] = v[:MAX_START_URLS]
            if n1 < n0:
                touched.append(key)

    for key, ceil in INPUT_CEILINGS.items():
        v = clamped.get(key)
        if isinstance(v, (int, float)) and v > ceil:
            clamped[key] = ceil
            touched.append(key)
    return clamped, sorted(set(touched))


# --- catalogue -----------------------------------------------------------

def list_actors(token: str, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    out = _request("GET", "/acts", token=token,
                   params={"limit": limit, "offset": offset})
    return [normalize_actor(a) for a in (out.get("data") or {}).get("items", [])]


def normalize_actor(a: Dict[str, Any]) -> Dict[str, Any]:
    name = a.get("name") or a.get("username", "?") + "/?"
    stats = a.get("stats") or {}
    dro = a.get("defaultRunOptions") or {}
    return {
        "id": a.get("id"),
        "name": a.get("username", "") + "/" + name,
        "title": a.get("title") or name,
        "description": (a.get("description") or "")[:300],
        "public": bool(stats.get("totalRuns")),
        "total_runs": stats.get("totalRuns"),
        "is_public": not a.get("isPrivate", True),
        # Mémoire choisie par l'auteur de l'acteur (8192 chez les crawlers
        # navigateur) — base de la recommandation, plafonnée par la gateway
        "author_memory_mbytes": dro.get("memoryMbytes"),
    }


def resolve_actor(actor_input: str, token: str) -> Dict[str, Any]:
    """actor_input can be 'user/name', 'user~name', '~name' (own), or an actor id."""
    act = actor_input.strip().lstrip("~").replace("/", "~")
    out = _request("GET", f"/acts/{act}", token=token)
    return normalize_actor(out.get("data") or {})


# --- exécution -----------------------------------------------------------

def run_actor_sync(actor: str, run_input: Optional[Dict[str, Any]] = None,
                   token: str = "", timeout_s: int = DEFAULT_TIMEOUT_S,
                   memory_mbytes: Optional[int] = None,
                   max_items: int = MAX_ITEMS_DEFAULT,
                   fetch_cost: bool = True) -> Dict[str, Any]:
    """Blocking run: POST run-sync-get-dataset-items.

    NEVER retried on failure by callers: once the run starts, Apify bills
    compute even if our connection drops mid-flight.
    run_input is clamped (see clamp_run_input) BEFORE the call — margin guard.
    After success, best-effort fetch of real usage (usage.totalUsd).
    """
    token = token or get_token()
    safe_input, touched_keys = clamp_run_input(run_input)
    # L'API Apify exige le séparateur '~' pour les runs (user~name), pas '/'
    act = actor.strip().replace("/", "~")
    path = f"/acts/{act}/run-sync-get-dataset-items"
    params: Dict[str, Any] = {"timeout": timeout_s, "limit": max_items}
    # Garde-fou mémoire : jamais sous le plancher, plafonné à 4096 MB.
    # Sans ça Apify alloue 8192 MB par défaut → compute x8 → marge détruite.
    mem = memory_mbytes or DEFAULT_MEMORY_MBYTES
    if mem < MIN_MEMORY_MBYTES:
        mem = MIN_MEMORY_MBYTES
    params["memory"] = min(mem, 4096)
    started = time.time()
    raw = _request("POST", path, token=token, params=params,
                   body=safe_input, timeout=timeout_s + 15)
    elapsed = round(time.time() - started, 2)

    items = raw if isinstance(raw, list) else []

    # Coût réel : le run-sync renvoie les items, pas le run record.
    # On récupère le dernier run fini de l'acteur (best-effort).
    usage_usd, run_id = -1.0, None
    if fetch_cost and items is not None:
        try:
            runs = _request("GET", f"/acts/{act}/runs", token=token,
                            params={"limit": 1, "status": "SUCCEEDED"},
                            timeout=15)
            first = ((runs.get("data") or {}).get("items") or [None])[0]
            if first:
                run_id = first.get("id")
                # coût réel : /v2/actor-runs/{id} -> usage.totalUsd quand
                # fourni ; sinon estimation ACTOR_COMPUTE_UNITS x $0.4/CU
                rec = _request("GET", f"/actor-runs/{run_id}", token=token,
                               timeout=15).get("data") or {}
                u = rec.get("usage") or {}
                v = u.get("totalUsd")
                cu = u.get("ACTOR_COMPUTE_UNITS")
                if v is not None:
                    usage_usd = float(v)
                elif cu is not None:
                    usage_usd = round(float(cu) * APIFY_CU_PRICE_USD, 6)
        except Exception:
            pass  # -1.0 reste le flag réconciliation

    return {
        "actor": act,
        "items": items,
        "item_count": len(items),
        "elapsed_s": elapsed,
        "usage_total_usd": usage_usd,   # coût réel Apify ; -1.0 = à réconcilier
        "apify_run_id": run_id,
        "clamped_inputs": touched_keys, # transparence vers le client
        "truncated_note": ("limit appliqué" if len(items) == max_items else None),
    }
