"""SafeClient — provider-agnostic safe caller for ANY AgentCash / x402 endpoint.

Generalized from the prospecting adapter. Payment is delegated to the local
AgentCash CLI (`agentcash fetch`); this layer adds budget caps, idempotency,
a cost ledger, and disciplined timeout handling.

Retry policy is deliberately conservative: a timed-out or failed *paid* call is
NOT auto-retried by default (that is exactly what double-charged us). Opt in with
max_retries only if you trust the server to honor the Idempotency-Key header.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

from .ledger import CostEvent, Ledger


class AgentCashError(RuntimeError):
    pass


class BudgetExceeded(AgentCashError):
    """Raised BEFORE a call that would push cumulative spend past the cap."""


def _default_runner(args, timeout):
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return proc.stdout, proc.returncode


class SafeClient:
    def __init__(self, binary=None,
                 default_max_amount: float = 1.0, default_timeout_ms: int = 150000,
                 max_budget_usd: float | None = None, idempotent: bool = True,
                 max_retries: int = 0, ledger: Ledger | None = None, runner=None):
        if binary:
            self.cmd = binary.split()
        elif shutil.which("agentcash"):
            self.cmd = ["agentcash"]
        else:
            self.cmd = ["npx", "agentcash@latest"]
        self.default_max_amount = default_max_amount
        self.default_timeout_ms = default_timeout_ms
        self.max_budget_usd = max_budget_usd
        self.idempotent = idempotent
        self.max_retries = max_retries
        self.ledger = ledger or Ledger()
        self.runner = runner or _default_runner
        self._cache = {}

    def call(self, url: str, *, method: str = "POST", body: dict | None = None,
             max_amount: float | None = None, timeout_ms: int | None = None,
             expected_price: float | None = None, account: str | None = None) -> dict:
        origin, endpoint = _origin_of(url), _endpoint_of(url)
        idem = _idempotency_key(method, endpoint, body)
        price = expected_price or 0.0

        if self.idempotent and idem in self._cache:
            self.ledger.record(CostEvent(endpoint, origin, 0.0, paid=False,
                                         tx_hash="cached", account=account))
            return self._cache[idem]

        if self.max_budget_usd is not None and self.ledger.total_usd + price > self.max_budget_usd + 1e-9:
            raise BudgetExceeded(
                f"budget cap ${self.max_budget_usd:.2f} reached "
                f"(spent ${self.ledger.total_usd:.2f}, next ${price:.2f})")

        args = list(self.cmd) + [
            "fetch", url, "-m", method, "--format", "json",
            "--max-amount", str(max_amount or self.default_max_amount),
            "--timeout", str(timeout_ms or self.default_timeout_ms),
            "-H", f"Idempotency-Key: {idem}",
        ]
        if body is not None:
            args += ["-b", json.dumps(body, ensure_ascii=False)]

        data, paid_price, tx = self._run(args, endpoint, price, timeout_ms)
        self.ledger.record(CostEvent(endpoint, origin, paid_price, paid=True,
                                     tx_hash=tx, account=account))
        if self.idempotent:
            self._cache[idem] = data
        return data

    def _run(self, args, endpoint, fallback_price, timeout_ms):
        sub_timeout = (timeout_ms or self.default_timeout_ms) / 1000 + 30
        attempt = 0
        while True:
            try:
                stdout, code = self.runner(args, sub_timeout)
            except subprocess.TimeoutExpired:
                # We do not know if payment settled — never blindly retry a paid call.
                raise AgentCashError(f"[{endpoint}] timed out; not retrying a possibly-paid call")
            if code == 0:
                return _parse_cli_output(stdout, fallback_price)
            err = (stdout or "").strip()
            if attempt < self.max_retries:      # opt-in only; relies on Idempotency-Key
                attempt += 1
                continue
            raise AgentCashError(f"[{endpoint}] {err or 'call failed'}")


# --- helpers ------------------------------------------------------------------

def _idempotency_key(method, endpoint, body) -> str:
    payload = json.dumps(body or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{method}|{endpoint}|{payload}".encode()).hexdigest()[:32]


def _origin_of(url: str) -> str:
    after = url.split("://", 1)
    scheme = after[0] if len(after) == 2 else "https"
    host = after[-1].split("/", 1)[0]
    return f"{scheme}://{host}"


def _endpoint_of(url: str) -> str:
    after_scheme = url.split("://", 1)[-1]
    path = after_scheme.split("/", 1)[1] if "/" in after_scheme else ""
    parts = [p for p in path.split("?")[0].split("/") if p]
    if parts and parts[0] == "api":
        parts = parts[1:]
    return "/".join(parts) or url


def _iter_json_objects(text: str):
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        yield obj
        i = end


def _parse_cli_output(stdout: str, fallback_price: float):
    objs = list(_iter_json_objects(stdout))
    data = objs[0] if objs else {}
    price, tx = fallback_price, None
    for o in objs:
        if not isinstance(o, dict):
            continue
        p = o.get("price")
        if isinstance(p, str):
            try:
                price = float(p.replace("$", "").strip())
            except ValueError:
                pass
        pay = o.get("payment")
        if isinstance(pay, dict) and pay.get("transactionHash"):
            tx = pay["transactionHash"]
    return data, price, tx
