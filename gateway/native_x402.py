"""
Client x402 natif Python — direct httpx + signature EVM (Base mainnet).
Évite le subprocess Node/AgentCash.
"""
import os
import json
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

import httpx

X402_PRIVATE_KEY_ENV = "X402_WALLET_KEY"
BASE_MAINNET = "eip155:8453"
DEFAULT_TIMEOUT_S = 45
KEY_FILE = os.path.expanduser("~/agentcash-web2-gateway/hotwallet.key")
HOT_WALLET_FLOOR_USD = 1.0

def _get_signer():
    from eth_account import Account
    key = os.environ.get(X402_PRIVATE_KEY_ENV)
    if not key and os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            key = f.read().strip()
    if not key:
        raise RuntimeError(
            f"Wallet natif indisponible: {X402_PRIVATE_KEY_ENV} non définie et {KEY_FILE} absent"
        )
    return Account.from_key(key)

def _build_payment_client(max_budget_usd: float = 1.0):
    from x402 import x402Client
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.http.clients import x402HttpxClient

    signer = _get_signer()
    payment = x402Client()
    payment.register(BASE_MAINNET, ExactEvmScheme(signer))
    return x402HttpxClient(client=payment)

async def call_x402(url: str, method: str = "GET",
                    body: Optional[Dict[str, Any]] = None) -> Tuple[int, Dict[str, Any], float]:
    client = _build_payment_client()
    try:
        balance = await get_wallet_balance()
        if balance < HOT_WALLET_FLOOR_USD:
            return 402, {"error": f"Hot wallet sous le seuil ({balance:.2f} USDC < {HOT_WALLET_FLOOR_USD}). Recharger l'adresse."}, 0.0

        async with client as pay_client:
            if method.upper() in ("POST", "PUT", "PATCH"):
                resp = await pay_client.request(method.upper(), url, json=body or {})
            else:
                resp = await pay_client.get(url)
            await resp.aread()
            status = resp.status_code
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:2000]}
            cost = _extract_cost(resp)
            return status, data, cost
    except httpx.TimeoutException:
        return 504, {"error": "x402 native call timed out — no retry (double-charge risk)"}, 0.0
    except Exception as e:
        return 502, {"error": f"x402 native call failed: {type(e).__name__}", "details": str(e)[:500]}, 0.0

def _extract_cost(resp) -> float:
    import base64
    hdr = resp.headers.get("x-payment-response")
    if not hdr:
        return -1.0
    try:
        receipt = json.loads(base64.b64decode(hdr))
        for k in ("amount", "maxAmountRequired"):
            v = receipt.get(k) or (receipt.get("payment", {}) or {}).get(k)
            if v is not None:
                val = float(v)
                return round(val / 1_000_000 if val > 100_000 else val, 6)
        return -1.0
    except Exception:
        return -1.0

async def get_wallet_balance(rpc_url: str = None) -> float:
    rpc = rpc_url or os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    signer = _get_signer()
    USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    balance_of_data = "0x70a08231" + signer.address[2:].lower().rjust(64, "0")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(rpc, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": USDC, "data": balance_of_data}, "latest"]})
        try:
            return int(r.json()["result"], 16) / 1_000_000
        except Exception:
            return 0.0
