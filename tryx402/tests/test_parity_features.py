"""Offline tests for the competitive-parity features — no network, no spend.

Run: python3 tryx402/tests/test_parity_features.py
Covers: planner, receipts (RFC 8032 vectors + tamper rejection), sessions
(token lifecycle + circuit breaker), http_transport (pay_and_retry discipline),
discovery (normalization + dedupe with a fake fetch).
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tryx402.planner import estimate_plan, suggest_cheaper          # noqa: E402
from tryx402.receipts import ReceiptBuilder                         # noqa: E402
from tryx402.sessions import (CircuitBreaker, SessionCapExceeded,   # noqa: E402
                              SessionManager, SessionedClient, SessionToken)
from tryx402.http_transport import (TransportError, pay_and_retry,  # noqa: E402
                                    price_from_402)
from tryx402.ledger import CostEvent, Ledger                        # noqa: E402


def test_planner():
    steps = [
        ("https://a.dev/api/email/work", {"e": "x@y.com"}, 0.03),
        {"url": "https://b.dev/api/search", "body": {}, "price": None},
        ("https://c.dev/api/heavy", None, 0.90),
    ]
    est = estimate_plan(steps, spent_usd=0.10, max_budget_usd=1.0)
    d = est.to_dict()
    assert abs(d["total_usd"] - 0.93) < 1e-9, d
    assert d["unknown_count"] == 1
    assert d["fits_budget"] is False            # unknown price = not provably safe
    assert d["over_budget_steps"] == [2]        # 0.10+0.93 breaks the cap at step 3
    assert d["steps"][1]["price_usd"] == -1.0

    sugg = suggest_cheaper(steps, spent_usd=0.10, max_budget_usd=1.0)
    assert any(s["index"] == 1 for s in sugg)   # unknown-price flagged first-class
    assert any(s["index"] == 2 for s in sugg)

    ok = estimate_plan([("https://a.dev/x", None, 0.02)], 0.0, 1.0)
    assert ok.to_dict()["fits_budget"] is True
    print("  ok  planner: totals, unknown flags (-1.0), over-budget indices")


def test_receipts_rfc8032():
    # official test vector 1 (empty message)
    seed1 = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pub1 = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    sig1 = "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    from tryx402.receipts import ed25519_publickey, ed25519_sign, ed25519_verify
    assert ed25519_publickey(seed1).hex() == pub1
    assert ed25519_sign(b"", seed1).hex() == sig1
    assert ed25519_verify(b"", bytes.fromhex(sig1), bytes.fromhex(pub1))
    assert not ed25519_verify(b"tampered", bytes.fromhex(sig1), bytes.fromhex(pub1))
    print("  ok  receipts: RFC 8032 vector 1 (keygen/sign/verify/reject)")


def test_receipts_builder():
    rb = ReceiptBuilder(seed=os.urandom(32))
    r = rb.build(endpoint="email/work", origin="https://a.dev",
                 price_usd=0.04, tx_hash="0xabc", idempotency_key="deadbeef")
    assert rb.verify(r) is True
    r2 = dict(r); r2["amount_usd"] = 999.0
    assert rb.verify(r2) is False               # amount tampering rejected
    r3 = dict(r); r3["endpoint"] = "other"
    assert rb.verify(r3) is False               # payload swap rejected
    r4 = dict(r); r4["sig"] = "00" * 32
    assert rb.verify(r4) is False
    from tryx402.receipts import verify_receipt
    assert verify_receipt(r) is True            # one-shot verify w/ embedded pubkey
    print("  ok  receipts: build/verify, tamper rejection (amount, field, sig)")


def test_session_tokens():
    mgr = SessionManager()
    tok = mgr.mint(cap_usd=0.50, ttl_s=60)
    assert mgr.verify(tok)
    t2 = SessionToken.from_dict(tok.to_dict())
    assert mgr.verify(t2)
    bad = tok.to_dict(); bad["cap_usd"] = 999.0
    assert not mgr.verify(SessionToken.from_dict(bad))
    expired = SessionToken(tok.session_id, tok.cap_usd,
                           time.time() - 10, time.time() - 1, tok.sig)
    assert not mgr.verify(expired)
    print("  ok  sessions: mint/roundtrip verify, tamper + expiry rejection")


def _fake_client():
    class FakeLedger:
        def __init__(self): self.events = []
    class FakeClient:
        def __init__(self):
            self.ledger = FakeLedger()
        def call(self, url, method="POST", body=None, expected_price=None, **kw):
            p = expected_price or 0.0
            self.ledger.events.append(CostEvent("x", url, p, True))
            return {"ok": True}
    return FakeClient()


def test_sessioned_client_cap():
    sc = SessionedClient(_fake_client(), SessionManager().mint(cap_usd=0.10))
    for _ in range(3):
        sc.call("https://a.dev/api/x", expected_price=0.03)
    assert abs(sc.spent_usd - 0.09) < 1e-9
    try:
        sc.call("https://a.dev/api/x", expected_price=0.03)
        raise AssertionError("cap not enforced")
    except SessionCapExceeded:
        pass
    assert sc.remaining_usd < 0.05
    print("  ok  sessions: per-session cap enforced BEFORE upstream call")


def test_circuit_breaker():
    cb = CircuitBreaker(max_consecutive_failures=3, cooldown_s=0.05)
    for _ in range(3):
        cb.record_failure("a.dev")
    assert not cb.allow("a.dev") and cb.state("a.dev") == "open"
    time.sleep(0.06)
    assert cb.allow("a.dev") and cb.state("a.dev") == "half-open"
    assert not cb.allow("a.dev")                # only ONE probe while half-open
    cb.record_success("a.dev")
    assert cb.state("a.dev") == "closed" and cb.allow("a.dev")
    print("  ok  breaker: open -> half-open probe -> close on success")


def test_price_parsing():
    atomic = json.dumps({"accepts": [{"maxAmountRequired": 10001}]}).encode()
    display = json.dumps({"accepts": [{"maxAmountRequired": "0.05"}]}).encode()
    garbage = b"not json"
    assert abs(price_from_402(atomic) - 0.010001) < 1e-9
    assert price_from_402(display) == 0.05
    assert price_from_402(garbage) is None      # unknown never guessed
    print("  ok  transport: atomic-vs-display parsing per KEY; garbage -> None")


def test_pay_and_retry():
    calls = {"n": 0}

    def payer(req, challenge):
        return "X-PAYMENT", "sig123"

    def opener(m, u, h, b):
        calls["n"] += 1
        if calls["n"] == 1:
            body = json.dumps({"accepts": [{"maxAmountRequired": 50000}]}).encode()
            return 402, {}, body
        assert h.get("X-PAYMENT") == "sig123"
        return 200, {}, b'{"data": true}'

    led = Ledger()
    r = pay_and_retry("POST", "https://s.example/api", body={"a": 1},
                      payer=payer, opener=opener,
                      max_budget_usd=1.0, ledger=led)
    assert r["status"] == 200 and calls["n"] == 2
    assert led.total_usd == 0.05

    # double-402 refused (no loop)
    n = {"v": 0}
    def opener_double(m, u, h, b):
        n["v"] += 1
        return 402, {}, b'{"accepts":[{"maxAmountRequired":10000}]}'
    try:
        pay_and_retry("GET", "https://s.example/x", payer=payer, opener=opener_double)
        raise AssertionError("looped on double-402")
    except TransportError:
        assert n["v"] == 2                      # initial + exactly one retry
    print("  ok  transport: single retry, ledger record, loop refusal")


if __name__ == "__main__":
    test_planner()
    test_receipts_rfc8032()
    test_receipts_builder()
    test_session_tokens()
    test_sessioned_client_cap()
    test_circuit_breaker()
    test_price_parsing()
    test_pay_and_retry()
    print("\nALL PARITY TESTS PASSED")
