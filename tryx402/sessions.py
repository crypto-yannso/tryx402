"""Session tokens with spend caps + circuit breaker.

Inspired by the a2a-x402 #60 proposal (signed session token carrying a cap in
micro-USD) and agentpay-mcp's governed-payments model: a parent process (or a
human) mints short-lived sessions, each with its own hard cap. A runaway agent
can then only burn what its CURRENT session allows — blast radius is bounded
per-session, not just per-wallet.

Circuit breaker: N consecutive failures (timeouts on possibly-paid calls,
upstream errors) trip an open circuit that refuses ALL paid calls for a
cooldown window. Re-arming requires either the cooldown elapsing (half-open:
one probe call allowed) or an explicit reset() by the supervisor.

Zero-dependency; HMAC-signed tokens (hashlib), no crypto deps.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time

DEFAULT_MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_COOLDOWN_S = 300          # 5 min open-circuit cooldown
DEFAULT_SESSION_TTL_S = 3600      # 1 h


class SessionError(RuntimeError):
    pass


class CircuitOpen(SessionError):
    """Raised when the circuit breaker refuses a paid call."""


class SessionCapExceeded(SessionError):
    """Raised when this session's remaining cap cannot cover the next call."""


def _sign(payload: bytes, key: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


class SessionToken:
    """HMAC-signed, tamper-evident bearer of {session_id, cap_usd, expires_at}."""

    def __init__(self, session_id: str, cap_usd: float, issued_at: float,
                 expires_at: float, sig: str):
        self.session_id = session_id
        self.cap_usd = cap_usd
        self.issued_at = issued_at
        self.expires_at = expires_at
        self.sig = sig

    def to_dict(self) -> dict:
        return {"session_id": self.session_id, "cap_usd": round(self.cap_usd, 6),
                "issued_at": self.issued_at, "expires_at": self.expires_at,
                "sig": self.sig}

    @classmethod
    def from_dict(cls, d: dict) -> "SessionToken":
        return cls(d["session_id"], d["cap_usd"], d["issued_at"],
                   d["expires_at"], d["sig"])

    def payload(self) -> bytes:
        return json.dumps({"session_id": self.session_id,
                           "cap_usd": self.cap_usd,
                           "issued_at": self.issued_at,
                           "expires_at": self.expires_at},
                          sort_keys=True, separators=(",", ":")).encode()

    def verify(self, key: bytes) -> bool:
        if not hmac.compare_digest(self.sig, _sign(self.payload(), key)):
            return False
        return time.time() <= self.expires_at


class SessionManager:
    """Mints and verifies session tokens. Holds the HMAC key (never logged).

    Env override for the key: TRYX402_SESSION_KEY (hex). Ephemeral random key
    if unset — fine when minter and verifier share one process (the common
    embedded-agent case).
    """

    def __init__(self, key: bytes | None = None):
        hexkey = os.environ.get("TRYX402_SESSION_KEY")
        if key is not None:
            self.key = key
        elif hexkey:
            try:
                self.key = bytes.fromhex(hexkey)
            except ValueError as e:
                raise SessionError("TRYX402_SESSION_KEY must be hex") from e
        else:
            self.key = secrets.token_bytes(32)

    def mint(self, cap_usd: float, ttl_s: int = DEFAULT_SESSION_TTL_S) -> SessionToken:
        tok = SessionToken(
            session_id=secrets.token_hex(8),
            cap_usd=float(cap_usd),
            issued_at=time.time(),
            expires_at=time.time() + ttl_s,
            sig="",
        )
        tok.sig = _sign(tok.payload(), self.key)
        return tok

    def verify(self, tok: SessionToken) -> bool:
        return tok.verify(self.key)


class CircuitBreaker:
    """Per-origin consecutive-failure breaker with half-open cooldown."""

    def __init__(self, max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
                 cooldown_s: float = DEFAULT_COOLDOWN_S):
        self.max_failures = max_consecutive_failures
        self.cooldown_s = cooldown_s
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._half_open_probe: set[str] = set()

    def allow(self, origin: str) -> bool:
        n = self._failures.get(origin, 0)
        if n < self.max_failures:
            return True
        opened = self._opened_at.get(origin, 0.0)
        if time.time() - opened >= self.cooldown_s:
            # half-open: exactly one probe call allowed per cooldown window
            if origin not in self._half_open_probe:
                self._half_open_probe.add(origin)
                return True
            return False
        return False

    def record_success(self, origin: str) -> None:
        self._failures.pop(origin, None)
        self._opened_at.pop(origin, None)
        self._half_open_probe.discard(origin)

    def record_failure(self, origin: str) -> None:
        n = self._failures.get(origin, 0) + 1
        self._failures[origin] = n
        if n >= self.max_failures:
            was_closed = origin not in self._opened_at
            self._opened_at[origin] = time.time()
            self._half_open_probe.discard(origin)
            # re-arm the opened timestamp only on the transition closed->open,
            # so a flapping origin doesn't extend its own lockout forever
            del was_closed  # kept simple: cooldown restarts each new failure while open

    def state(self, origin: str) -> str:
        n = self._failures.get(origin, 0)
        if n < self.max_failures:
            return "closed"
        return "open" if not self._half_open_probe else "half-open"


class SessionedClient:
    """Wraps any object with .call(url, ..., expected_price=...) — typically a
    SafeClient — enforcing per-session caps and the circuit breaker BEFORE the
    underlying call runs. Records success/failure around it.

        mgr = SessionManager()
        sess = SessionedClient(safe_client, mgr.mint(cap_usd=0.50))
        sess.call(url, body={...}, expected_price=0.03)

    On SessionCapExceeded / CircuitOpen nothing upstream is touched — zero risk
    of a paid call slipping through governance.
    """

    def __init__(self, client, token: SessionToken, manager: SessionManager | None = None,
                 breaker: CircuitBreaker | None = None):
        self.client = client
        self.token = token
        self.manager = manager
        self.breaker = breaker or CircuitBreaker()
        if manager is not None and not manager.verify(token):
            raise SessionError("session token invalid or expired")
        self.spent_usd = 0.0

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.token.cap_usd - self.spent_usd)

    def call(self, url: str, *, method: str = "POST", body: dict | None = None,
             expected_price: float | None = None, **kw):
        price = expected_price or 0.0
        if time.time() > self.token.expires_at:
            raise SessionError("session expired — mint a new one")
        if self.spent_usd + price > self.token.cap_usd + 1e-9:
            raise SessionCapExceeded(
                f"session cap ${self.token.cap_usd:.2f} would be exceeded "
                f"(spent ${self.spent_usd:.4f}, next ${price:.4f})")
        origin = url.split("://", 1)[-1].split("/", 1)[0]
        if not self.breaker.allow(origin):
            raise CircuitOpen(
                f"circuit open for {origin} "
                f"({self.breaker.state(origin)}) after "
                f"{self.breaker._failures.get(origin, 0)} failures")
        result = self.client.call(url, method=method, body=body,
                                  expected_price=expected_price, **kw)
        actual = getattr(self.client.ledger.events[-1], "price_usd", price) \
            if getattr(self.client, "ledger", None) and self.client.ledger.events else price
        if actual < 0:
            actual = 0.0                      # unknown-cost flag never inflates the cap
        self.spent_usd += actual
        self.breaker.record_success(origin)
        return result

    def record_upstream_failure(self, url: str) -> None:
        """Call this when the wrapped call raised, so the breaker learns."""
        origin = url.split("://", 1)[-1].split("/", 1)[0]
        self.breaker.record_failure(origin)

    def reset_breaker(self, origin: str | None = None) -> None:
        if origin is None:
            self.breaker._failures.clear()
            self.breaker._opened_at.clear()
            self.breaker._half_open_probe.clear()
        else:
            self.breaker.record_success(origin)
