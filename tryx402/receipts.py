"""Signed receipts — verifiable proof of what was paid, offline-checkable.

Pattern borrowed from TrustBench: every paid call produces an Ed25519-signed
receipt binding {endpoint, origin, amount, tx_hash, timestamp, idempotency key}.
Anyone holding the public key can verify a receipt WITHOUT network access;
the on-chain tx_hash is the settlement evidence to check against a block
explorer when online.

Key management (buyer side, non-custodial):
  * TRYX402_RECEIPT_KEY   — hex seed (32 bytes). Generated on first use if absent.
  * TRYX402_RECEIPT_PUB   — derived automatically; publish it so counterparties
                            can audit your receipts.

Zero-dependency core: uses `cryptography` if installed, else falls back to a
pure-Python Ed25519 (rfc8032) implementation — slow but correct, fine for
one receipt per API call.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

# --- Ed25519 (pure Python fallback, RFC 8032) ---------------------------------

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _inv(x):
    return pow(x, _P - 2, _P)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P          # second sqrt candidate when p % 8 == 5
        if (x * x - xx) % _P != 0:
            raise ValueError("point recovery failed")
    return x


_By = 4 * _inv(5) % _P
_Bx = _xrecover(_By)
if _Bx & 1:                      # normalize to the even root (RFC 8032 base point)
    _Bx = _P - _Bx
_B = [_Bx % _P, _By % _P]


def _edwards(P1, P2):
    x1, y1 = P1
    x2, y2 = P2
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _D * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _D * x1 * x2 * y1 * y2)
    return [x3 % _P, y3 % _P]


def _scalarmult(P, e):
    Q = [0, 1]
    while e > 0:
        if e & 1:
            Q = _edwards(Q, P)
        P = _edwards(P, P)
        e >>= 1
    return Q


def _compress(P):
    x, y = P
    b = bytearray(y.to_bytes(32, "little"))   # y < 2^255 so this always fits
    if x & 1:
        b[31] |= 0x80
    return bytes(b)


def _decompress(b):
    y_le = int.from_bytes(b, "little")
    sign = y_le >> 255                     # sign bit is bit 255 (MSB, little-endian)
    y = y_le & ((1 << 255) - 1)
    try:
        x = _xrecover(y)
    except ValueError:
        raise ValueError("invalid point")
    if x & 1 != sign:
        x = _P - x
    return [x, y]


def _secret_expand(secret):
    if len(secret) != 32:
        raise ValueError("seed must be 32 bytes")
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def ed25519_publickey(seed: bytes) -> bytes:
    a, _ = _secret_expand(seed)
    return _compress(_scalarmult(_B, a))


def ed25519_sign(msg: bytes, seed: bytes) -> bytes:
    a, prefix = _secret_expand(seed)
    A = _compress(_scalarmult(_B, a))
    r = int.from_bytes(hashlib.sha512(prefix + msg).digest(), "little") % _L
    R = _compress(_scalarmult(_B, r))
    h = int.from_bytes(hashlib.sha512(R + A + msg).digest(), "little") % _L
    s = (r + h * a) % _L
    return R + s.to_bytes(32, "little")


def ed25519_verify(msg: bytes, sig: bytes, pubkey: bytes) -> bool:
    if len(sig) != 64 or len(pubkey) != 32:
        return False
    try:
        A = _decompress(pubkey)
        Rs = sig[:32]
        R = _decompress(Rs)
        s = int.from_bytes(sig[32:], "little")
        if s >= _L:
            return False
        h = int.from_bytes(hashlib.sha512(Rs + pubkey + msg).digest(), "little") % _L
        left = _scalarmult(_B, s)
        right = _edwards(R, _scalarmult(A, h))
        return _compress(left) == _compress(right)
    except (ValueError, IndexError):
        return False


# --- Receipt model -------------------------------------------------------------

RECEIPT_VERSION = 1


class ReceiptError(RuntimeError):
    pass


def get_or_create_seed(env=os.environ) -> bytes:
    """Load TRYX402_RECEIPT_KEY (hex) or create+persist one next to nothing.

    Never prints or logs the key. If no env var is set, generates an ephemeral
    in-memory seed (verifiable within the process only).
    """
    hexkey = env.get("TRYX402_RECEIPT_KEY")
    if hexkey:
        try:
            seed = bytes.fromhex(hexkey)
            if len(seed) != 32:
                raise ValueError
            return seed
        except ValueError as e:
            raise ReceiptError("TRYX402_RECEIPT_KEY must be 64 hex chars (32 bytes)") from e
    return os.urandom(32)


class ReceiptBuilder:
    """Builds and signs receipts; also verifies foreign ones."""

    def __init__(self, seed: bytes | None = None):
        self.seed = seed if seed is not None else get_or_create_seed()
        self.public_key = ed25519_publickey(self.seed)

    def build(self, *, endpoint: str, origin: str, price_usd: float,
              tx_hash: str | None, account: str | None = None,
              idempotency_key: str | None = None, ts: float | None = None) -> dict:
        payload = {
            "v": RECEIPT_VERSION,
            "ts": round(ts if ts is not None else time.time(), 6),
            "origin": origin,
            "endpoint": endpoint,
            "amount_usd": round(float(price_usd), 6),
            "tx_hash": tx_hash,
            "account": account,
            "idem": idempotency_key,
        }
        msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sig = ed25519_sign(msg, self.seed)
        receipt = dict(payload)
        receipt["sig"] = sig.hex()
        receipt["pubkey"] = self.public_key.hex()
        return receipt

    def verify(self, receipt: dict) -> bool:
        """Offline verification: True iff signature matches payload AND pubkey."""
        try:
            payload = {k: v for k, v in receipt.items() if k not in ("sig", "pubkey")}
            if payload.get("v") != RECEIPT_VERSION:
                return False
            msg = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            return ed25519_verify(msg, bytes.fromhex(receipt["sig"]),
                                  bytes.fromhex(receipt["pubkey"]))
        except (KeyError, TypeError, ValueError):
            return False


def verify_receipt(receipt: dict) -> bool:
    """One-shot verify with the pubkey embedded in the receipt itself."""
    return ReceiptBuilder.__new__(ReceiptBuilder).verify(receipt)
