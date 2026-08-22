"""Offline tests for the gateway core — no spend, no subprocess (the CLI runner
is injected/faked). Run: python3 gateway/tests/test_gateway.py
"""
from __future__ import annotations

from gateway.accounts import (
    AccountStore, FxRates, InsufficientBalance, format_amount, price_minor,
)
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


def test_billing_multicurrency():
    rates = FxRates()
    # same $0.04 upstream, 30% margin, billed in each customer's own currency:
    assert price_minor(0.04, "EUR", rates, 0.30) == 5    # 0.04*0.92*1.3 = 0.04784 -> 5c
    assert price_minor(0.04, "USD", rates, 0.30) == 6    # 0.04*1.00*1.3 = 0.052   -> 6c
    assert price_minor(0.04, "JPY", rates, 0.30) == 8    # 0.04*150*1.3 = 7.8 yen  -> 8 (0 decimals)

    store = AccountStore(path="/tmp/gw_test_none.json")
    store.create("acme", currency="GBP", margin=0.30)
    store.fund("acme", 20)                                # 20.00 GBP -> 2000 minor
    assert store.accounts["acme"].balance_minor == 2000
    charge = store.authorize("acme", 0.04, rates)        # GBP 0.04*0.79*1.3=0.041 -> 5p
    store.charge("acme", charge)
    assert store.accounts["acme"].balance_minor == 2000 - charge
    assert format_amount(store.accounts["acme"].balance_minor, "GBP") == "19.95 GBP"

    store.accounts["acme"].balance_minor = 2             # now too low
    try:
        store.authorize("acme", 0.04, rates)
        assert False, "should have raised InsufficientBalance"
    except InsufficientBalance:
        pass
    print("  ok  billing: EUR/USD/JPY/GBP minor units, margin, insufficient-balance guard")


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


def test_stripe_webhook():
    import hashlib
    import hmac
    import json as _json
    import time

    from gateway.accounts import AccountStore
    from gateway.stripe_integration import WebhookError, handle_event, verify_webhook

    secret = "whsec_test"
    event = {"type": "checkout.session.completed", "data": {"object": {
        "metadata": {"account_id": "acme"}, "amount_total": 2000, "currency": "eur"}}}
    payload = _json.dumps(event).encode()
    t = str(int(time.time()))
    good = hmac.new(secret.encode(), t.encode() + b"." + payload, hashlib.sha256).hexdigest()

    ev = verify_webhook(payload, f"t={t},v1={good}", secret)     # valid signature
    assert ev["type"] == "checkout.session.completed"
    try:
        verify_webhook(payload, f"t={t},v1=deadbeef", secret)    # forged signature
        assert False, "should have raised WebhookError"
    except WebhookError:
        pass

    store = AccountStore(path="/tmp/gw_stripe_test.json")
    store.create("acme", currency="EUR")
    handle_event(ev, store, save=False)                          # 2000c = 20.00 EUR credited
    assert store.accounts["acme"].balance_minor == 2000
    print("  ok  stripe webhook: HMAC verify + forgery reject + credit on checkout.completed")


def test_currency_mismatch_credit():
    from gateway.accounts import AccountStore, FxRates

    rates = FxRates()
    store = AccountStore(path="/tmp/gw_fx_test.json")
    # Existing USD account receives an EUR payment: converted EUR -> USD -> (acct) USD
    store.create("acme", currency="USD")
    with __import__("pytest").raises(ValueError):
        store.credit_minor("acme", 2000, "EUR")            # strict: no rates -> reject
    acct = store.credit_minor("acme", 2000, "EUR", rates=rates)
    # 20.00 EUR = 20/0.92 USD = 21.74 USD -> 2174 minor, ceil-rounded
    assert acct.balance_minor == 2174
    assert acct.currency == "USD"
    # New account: created denominated in the PAYMENT's currency
    acct2 = store.credit_minor("novo", 1500, "JPY", rates=rates)
    assert acct2.currency == "JPY" and acct2.balance_minor == 1500
    print("  ok  currency mismatch: strict reject without rates, FX convert with rates, auto-denominate new accounts")


def test_webhook_idempotency():
    from gateway.accounts import AccountStore
    from gateway.stripe_integration import handle_event

    event = {"id": "evt_123", "type": "checkout.session.completed",
             "data": {"object": {"metadata": {"account_id": "acme"},
                                 "amount_total": 1000, "currency": "eur"}}}
    store = AccountStore(path="/tmp/gw_idem_test.json")
    seen = set()
    handle_event(event, store, save=False, seen_ids=seen)
    handle_event(event, store, save=False, seen_ids=seen)   # replayed delivery
    assert store.accounts["acme"].balance_minor == 1000     # credited ONCE
    handle_event(event, store, save=False, seen_ids=None)   # no idempotency -> double
    assert store.accounts["acme"].balance_minor == 2000
    print("  ok  webhook idempotency: replay skipped when seen_ids given")


def test_billing_loop():
    """The fiat -> x402 loop: a paid upstream call debits the customer."""
    out = '{\"ok\":1}\n{\"price\":\"$0.04\",\"payment\":{\"transactionHash\":\"0x1\"}}'
    runner, calls = make_runner(out)
    from gateway.accounts import AccountStore, FxRates

    rates = FxRates()
    store = AccountStore(path="/tmp/gw_bill_test.json")
    store.create("acme", currency="EUR", margin=0.30)
    store.fund("acme", 10)                                   # 10.00 EUR = 1000 minor

    from gateway.client import Billing, SafeClient
    c = SafeClient(runner=runner, billing=Billing(store, rates))
    c.call("https://x/api/email/work", body={"a": 1}, expected_price=0.04, account="acme")
    # billed 0.04*0.92*1.3 = 4.784c -> ceil 5 minor
    assert store.accounts["acme"].balance_minor == 1000 - 5
    # now drain the account and expect InsufficientBalance on the next paid call
    store.accounts["acme"].balance_minor = 2
    try:
        c.call("https://x/api/email/work", body={"b": 2}, expected_price=0.04,
               account="acme")
        assert False, "should have raised InsufficientBalance"
    except Exception as e:
        assert type(e).__name__ in ("InsufficientBalance", "AgentCashError")
        # the upstream call DID happen and was recorded; the failure is surfaced
    assert calls["n"] == 2
    print("  ok  billing loop: customer debited at margin per real call, empty balance surfaces error")


def test_api_keys():
    from gateway.accounts import AccountStore

    store = AccountStore(path="/tmp/gw_keys_test.json")
    store.create("acme", currency="EUR")
    key = store.issue_api_key("acme")
    assert key.startswith("gw_")
    assert store.accounts["acme"].api_key_hash != key          # only hash stored
    assert store.authenticate(key).id == "acme"                # round-trip
    for bad in ("gw_forged", ""):
        try:
            store.authenticate(bad)
            raise AssertionError(f"should have raised PermissionError for {bad!r}")
        except PermissionError:
            pass
    print("  ok  api keys: hashed at rest, round-trip auth, forged/missing rejected")


if __name__ == "__main__":
    test_helpers()
    test_call_and_ledger()
    test_idempotency()
    test_budget_cap()
    test_billing_multicurrency()
    test_facade()
    test_stripe_webhook()
    test_currency_mismatch_credit()
    test_webhook_idempotency()
    test_billing_loop()
    test_api_keys()
    print("\nALL GATEWAY TESTS PASSED")
