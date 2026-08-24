"""Offline tests for the gateway core — no spend, no subprocess (the CLI runner
is injected/faked). Run: python3 gateway/tests/test_gateway.py
"""
from __future__ import annotations

from gateway.client import (
    BudgetExceeded, SafeClient, _endpoint_of, _idempotency_key, _origin_of,
)


def make_runner(stdout, code=0):
    calls = {"n": 0}

    def runner(args, timeout):
        calls["n"] += 1
        return stdout, code
    return runner, calls


def test_helpers():
    assert _origin_of("https://stable-deepline.dev/api/email/work") == "https://stable-deepline.dev"
    assert _endpoint_of("https://stable-deepline.dev/api/email/work") == "email/work"
    assert _idempotency_key("POST", "x", {"a": 1}) == _idempotency_key("POST", "x", {"a": 1})
    assert _idempotency_key("POST", "x", {"a": 1}) != _idempotency_key("POST", "x", {"a": 2})
    print("  ok  helpers (origin / endpoint / idempotency key)")


def test_call_and_ledger():
    out = '{"outputs":{"email":{"value":"x@y.com"}}}\n{"price":"$0.04","payment":{"transactionHash":"0xabc"}}'
    runner, calls = make_runner(out)
    c = SafeClient(runner=runner)
    data = c.call("https://stable-deepline.dev/api/email/work", body={"a": 1}, expected_price=0.04)
    assert data == {"outputs": {"email": {"value": "x@y.com"}}}
    assert c.ledger.total_usd == 0.04
    assert c.ledger.by_origin() == {"https://stable-deepline.dev": 0.04}
    assert calls["n"] == 1
    print("  ok  call parses data, records $0.04 + tx, groups by origin")


def test_idempotency():
    runner, calls = make_runner('{"ok":1}\n{"price":"$0.25"}')
    c = SafeClient(runner=runner)
    body = {"roles": ["X"], "domain": "a.com"}
    c.call("https://x/api/contacts/by-role", body=body, expected_price=0.25)
    c.call("https://x/api/contacts/by-role", body=body, expected_price=0.25)   # repeat
    assert calls["n"] == 1               # 2nd served from cache — no subprocess
    assert c.ledger.total_usd == 0.25    # paid once
    print("  ok  idempotency: repeat call not re-run, not re-paid")


def test_budget_cap():
    runner, calls = make_runner('{"ok":1}\n{"price":"$0.25"}')
    c = SafeClient(runner=runner, max_budget_usd=0.40)
    c.call("https://x/api/contacts/by-role", body={"d": 1}, expected_price=0.25)  # 0.25 ok
    try:
        c.call("https://x/api/contacts/by-role", body={"d": 2}, expected_price=0.25)  # 0.50 > 0.40
        assert False, "should have raised BudgetExceeded"
    except BudgetExceeded:
        pass
    assert calls["n"] == 1               # blocked BEFORE the paid call
    print("  ok  budget cap blocks before overspending")


def test_facade():
    from gateway import Gateway
    out = '{"data":{"status":"valid"}}\n{"price":"$0.03","payment":{"transactionHash":"0xdef"}}'
    runner, _ = make_runner(out)
    gw = Gateway(max_budget_usd=0.10, runner=runner)
    data = gw.call("https://stable-deepline.dev/api/email/validate",
                   body={"email": "x@y.com"}, price=0.03)
    assert data == {"data": {"status": "valid"}}
    assert gw.spent_usd == 0.03
    assert gw.spend_by_origin() == {"https://stable-deepline.dev": 0.03}
    print("  ok  Gateway facade: one-import call + spend tracking")


def test_on_paid_hook():
    """Hosted-billing callback: fired on the REAL paid price, per account."""
    events = []
    out = '{"ok":1}\n{"price":"$0.04","payment":{"transactionHash":"0x1"}}'
    runner, _ = make_runner(out)
    c = SafeClient(runner=runner, on_paid=lambda acct, usd: events.append((acct, usd)))
    c.call("https://x/api/email/work", body={"a": 1}, expected_price=0.04, account="acme")
    assert events == [("acme", 0.04)]
    # no account -> no hook
    c.call("https://x/api/email/work", body={"b": 2}, expected_price=0.04)
    assert len(events) == 1
    # failing callback surfaces the error after recording the call
    def boom(acct, usd):
        raise RuntimeError("billing down")
    c2 = SafeClient(runner=make_runner(out)[0], on_paid=boom)
    try:
        c2.call("https://x/api/email/work", body={"c": 3}, expected_price=0.04, account="acme")
        assert False, "should have raised"
    except RuntimeError:
        pass
    assert c2.ledger.total_usd > 0       # upstream call still recorded
    print("  ok  on_paid hook: real price, per-account, failure surfaced")


def test_quote_client():
    """quote() hits /v1/quote with the API key and parses the server's price."""
    from gateway import Gateway

    captured = {}

    class FakeResponse:
        status = 200
        def read(self):
            return b'{"account":"acme","currency":"EUR","charge_minor":5,"charge":"0.05 EUR"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["key"] = req.headers.get("X-api-key") or req.headers.get("X-API-Key")
        return FakeResponse()

    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        gw = Gateway(api_base="http://test", api_key="gw_test")
        q = gw.quote(0.04)
    finally:
        urllib.request.urlopen = orig

    assert "/v1/quote?data_cost_usd=0.04" in captured["url"]
    assert captured["key"] == "gw_test"
    assert q["charge_minor"] == 5 and q["currency"] == "EUR"
    # no key -> clear error, no HTTP call
    gw2 = Gateway(api_base="http://test")
    try:
        gw2.quote(0.04)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "API key" in str(e)
    print("  ok  quote(): server-side pricing call with API key, no-key guard")


def test_rate_limiter():
    from gateway.rate_limit import InMemoryRateLimiter

    rl = InMemoryRateLimiter()
    # 3 requêtes max par fenêtre de 1 seconde
    assert rl.check("user:1", max_requests=3, window_seconds=1.0) is True
    assert rl.check("user:1", max_requests=3, window_seconds=1.0) is True
    assert rl.check("user:1", max_requests=3, window_seconds=1.0) is True
    assert rl.check("user:1", max_requests=3, window_seconds=1.0) is False  # 4e rejetée

    # clé différente : isolée
    assert rl.check("user:2", max_requests=3, window_seconds=1.0) is True

    # reset
    rl.reset()
    assert rl.check("user:1", max_requests=3, window_seconds=1.0) is True
    print("  ok  rate limiter: sliding window, per-key isolation, block threshold & reset")


if __name__ == "__main__":
    test_helpers()
    test_call_and_ledger()
    test_idempotency()
    test_budget_cap()
    test_facade()
    test_on_paid_hook()
    test_quote_client()
    test_rate_limiter()
    print("\nALL GATEWAY TESTS PASSED")
