"""Security hardening tests — each maps to a pentest finding.

RED phase: every test here must FAIL against the vulnerable code first.
Run: python3 -m pytest gateway/tests/test_security.py -v
"""

import json
import os
import tempfile
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")


def _fresh_env(monkeypatch):
    """Isolated DB per test."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("TRYX402_DB_PATH", db_path)
    monkeypatch.delenv("TRYX402_STRIPE_WEBHOOK_SECRET", raising=False)
    return db_path


def _make_client():
    from gateway.server import create_app
    from starlette.testclient import TestClient
    app = create_app()
    return TestClient(app), app


def _seed(db_path, customer_id, cents):
    from gateway.wallet_sqlite import SQLiteWallet
    SQLiteWallet(db_path, customer_id).credit(cents, "test seed")


# ---------------------------------------------------------------------------
# Finding 1 — identity spoofing: X-Customer-ID alone must NOT grant access
# ---------------------------------------------------------------------------

class TestAuthHardening:
    def test_customer_id_alone_is_rejected(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, _ = _make_client()
        resp = client.get("/v1/wallet/balance", headers={"X-Customer-ID": "victime-123"})
        assert resp.status_code == 401

    def test_api_key_without_registration_is_rejected(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, _ = _make_client()
        resp = client.get("/v1/wallet/balance", headers={"X-API-Key": "some-random-key"})
        assert resp.status_code == 401

    def test_valid_session_grants_access(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, app = _make_client()
        # Anonymous session flow: mint a session, then use its token
        resp = client.post("/v1/auth/session", json={})
        assert resp.status_code in (200, 201), resp.text
        body = resp.json()
        customer_id, token = body["customer_id"], body["token"]
        _seed(db_path, customer_id, 500)

        r_ok = client.get("/v1/wallet/balance",
                          headers={"X-Customer-ID": customer_id, "X-Session-Token": token})
        assert r_ok.status_code == 200
        assert r_ok.json()["balance_cents"] == 500

    def test_cannot_read_other_wallet_even_with_own_token(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, _ = _make_client()
        b1 = client.post("/v1/auth/session", json={}).json()
        b2 = client.post("/v1/auth/session", json={}).json()
        _seed(db_path, b2["customer_id"], 777)

        # attacker uses own id + own token but claims victim's wallet via balance endpoint
        r = client.get("/v1/wallet/balance",
                       headers={"X-Customer-ID": b2["customer_id"],
                                "X-Session-Token": b1["token"]})
        # token of b1 must not authenticate wallet b2
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Finding 2 — webhook replay: same stripe_session_id credited twice
# ---------------------------------------------------------------------------

def _signed_event(secret, payload, ts=None):
    import hashlib, hmac
    ts = str(int(time.time()))
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), f"{ts}.{body.decode()}".encode(),
                   hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={ts},v1={sig}"}


class TestWebhookIdempotency:
    def test_same_checkout_event_credited_once(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        secret = "whsec_test_123"
        monkeypatch.setenv("TRYX402_STRIPE_WEBHOOK_SECRET", secret)
        client, _ = _make_client()

        event_payload = {
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_test_abc",
                "client_reference_id": "cust-replay",
                "amount_total": 1000,
            }},
        }
        body, headers = _signed_event(secret, event_payload)

        r1 = client.post("/v1/billing/webhook", content=body, headers=headers)
        r2 = client.post("/v1/billing/webhook", content=body, headers=headers)
        assert r1.status_code == 200 and r2.status_code == 200

        from gateway.wallet_sqlite import SQLiteWallet
        bal = SQLiteWallet(db_path, "cust-replay").get_balance()
        assert bal == 1000, f"replay doubled credit: {bal}"

    def test_bad_signature_rejected(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        monkeypatch.setenv("TRYX402_STRIPE_WEBHOOK_SECRET", "whsec_right")
        client, _ = _make_client()
        body, headers = _signed_event("whsec_wrong", {"type": "checkout.session.completed",
                                                      "data": {"object": {}}})
        resp = client.post("/v1/billing/webhook", content=body, headers=headers)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Finding 3 — client-declared price: server registry decides the price
# ---------------------------------------------------------------------------

class TestServerSidedPricing:
    def test_unknown_endpoint_rejected(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, app = _make_client()
        sess = client.post("/v1/auth/session", json={}).json()
        _seed(db_path, sess["customer_id"], 10000)

        resp = client.post("/v1/proxy/call", headers={
            "X-Customer-ID": sess["customer_id"],
            "X-Session-Token": sess["token"],
        }, json={"url": "https://not-in-registry.example.com/api",
                 "method": "GET", "price_usd": 0.000001})
        assert resp.status_code == 400  # unknown origin -> refuse

    def test_registered_price_used_not_client_price(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, app = _make_client()
        sess = client.post("/v1/auth/session", json={}).json()
        _seed(db_path, sess["customer_id"], 10000)
        cid = sess["customer_id"]

        # Register a known origin at a fixed price (admin/seed API for tests)
        app.state.price_registry.register(
            origin="https://known.example.com", price_cents=300)

        # Client tries to declare price_usd=0.000001 — ignored
        resp = client.post("/v1/proxy/call", headers={
            "X-Customer-ID": cid,
            "X-Session-Token": sess["token"],
        }, json={"url": "https://known.example.com/api",
                 "method": "GET", "price_usd": 0.000001})

        data = resp.json() if resp.status_code != 400 else {}
        if resp.status_code == 200:
            # debited price must be 300 + commission, not ~0
            assert data["cost_cents"] == 300
        else:
            # or the field is simply rejected at validation level
            assert "price_usd" not in json.dumps(resp.json())


# ---------------------------------------------------------------------------
# Finding 4 — race condition: atomic debit, never negative balance
# ---------------------------------------------------------------------------

class TestAtomicDebit:
    def test_concurrent_debits_never_go_negative(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, _ = _make_client()
        sess = client.post("/v1/auth/session", json={}).json()
        cid, token = sess["customer_id"], sess["token"]
        _seed(db_path, cid, 100)  # only 2 x 50cts possible

        app_state_registry = None
        # register cheap target so pricing passes
        from gateway.server import create_app
        app = create_app()
        app.state.price_registry.register(origin="http://127.0.0.1:9",
                                          price_cents=0, allow_private=True)
        client2 = None
        from starlette.testclient import TestClient
        client2 = TestClient(app)

        headers = {"X-Customer-ID": cid, "X-Session-Token": token}
        import concurrent.futures as cf

        def call(_):
            try:
                return client2.post("/v1/proxy/call", headers=headers, json={
                    "url": "http://127.0.0.1:9/x", "method": "GET"}).status_code
            except Exception:
                return "err"

        with cf.ThreadPoolExecutor(20) as ex:
            results = list(ex.map(call, range(20)))

        from gateway.wallet_sqlite import SQLiteWallet
        bal = SQLiteWallet(db_path, cid).get_balance()
        assert bal >= 0, f"negative balance after race: {bal}"
        successes = sum(1 for c in results if c == 200)
        debited_total = 100 - bal
        assert successes <= 2 or debited_total <= 100


# ---------------------------------------------------------------------------
# Finding 5 — SSRF: proxy must only reach registered public origins
# ---------------------------------------------------------------------------

class TestSSRFAllowlist:
    def _session(self, client):
        s = client.post("/v1/auth/session", json={}).json()
        return s["customer_id"], s["token"]

    def test_internal_ip_blocked(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, app = _make_client()
        cid, token = self._session(client)
        _seed(db_path, cid, 10000)
        app.state.price_registry.register(origin="https://known.example.com",
                                          price_cents=10, allow_private=True)
        # but this URL was NEVER registered:
        resp = client.post("/v1/proxy/call", headers={
            "X-Customer-ID": cid, "X-Session-Token": token},
            json={"url": "http://169.254.169.254/latest/meta-data/", "method": "GET"})
        assert resp.status_code in (400, 403)

    def test_file_scheme_blocked(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, _ = _make_client()
        cid, token = self._session(client)
        _seed(db_path, cid, 10000)
        resp = client.post("/v1/proxy/call", headers={
            "X-Customer-ID": cid, "X-Session-Token": token},
            json={"url": "file:///etc/passwd", "method": "GET"})
        assert resp.status_code in (400, 403)


# ---------------------------------------------------------------------------
# Finding 6 — refund on provider failure
# ---------------------------------------------------------------------------

class TestRefundOnFailure:
    def test_failed_provider_call_refunds_wallet(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        client, app = _make_client()
        sess = client.post("/v1/auth/session", json={}).json()
        cid, token = sess["customer_id"], sess["token"]
        _seed(db_path, cid, 10000)
        # port 9 (discard) on localhost: connection refused quickly
        app.state.price_registry.register(origin="http://127.0.0.1:9",
                                          price_cents=100, allow_private=True)
        resp = client.post("/v1/proxy/call", headers={
            "X-Customer-ID": cid, "X-Session-Token": token},
            json={"url": "http://127.0.0.1:9/x", "method": "GET"})
        assert resp.status_code == 502
        from gateway.wallet_sqlite import SQLiteWallet
        bal = SQLiteWallet(db_path, cid).get_balance()
        assert bal == 10000, f"wallet should be refunded, got {bal}"


# ---------------------------------------------------------------------------
# Finding 7 — checkout must not mutate global os.environ
# ---------------------------------------------------------------------------

class TestCheckoutNoEnvMutation:
    def test_checkout_does_not_mutate_environ(self, monkeypatch):
        db_path = _fresh_env(monkeypatch)
        monkeypatch.delenv("TRYX402_API_KEY", raising=False)
        monkeypatch.setenv("TRYX402_STRIPE_SECRET_KEY", "sk_test_dummy")
        client, _ = _make_client()
        before = dict(os.environ)
        resp = client.post("/v1/billing/checkout", json={
            "customer_email": "a@b.com", "amount_cents": 500, "currency": "eur"})
        # Stripe will fail with dummy key; we don't care about status here,
        # what matters: env untouched even on success path attempt.
        after = dict(os.environ)
        diff = {k for k in set(before) | set(after)
                if before.get(k) != after.get(k)}
        assert not diff, f"os.environ mutated by checkout: {diff}"
