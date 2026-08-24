"""Tests for anonymous auth (no API key, no email, no account).

The SDK generates a local customer_id and the server creates the wallet
on first contact. The only human action is the Stripe card payment.

Run: python3 -m pytest gateway/tests/test_anon_auth.py -v
"""

import json
import os
import tempfile
import pytest


class TestAnonAuth:
    """Tests for the zero-friction auth flow."""

    def test_sdk_generates_persistent_customer_id(self):
        from gateway.anon_auth import get_or_create_customer_id
        cid1 = get_or_create_customer_id()
        cid2 = get_or_create_customer_id()
        assert cid1 == cid2  # Same ID across calls
        assert len(cid1) > 0

    def test_customer_id_is_uuid_format(self):
        from gateway.anon_auth import get_or_create_customer_id
        cid = get_or_create_customer_id()
        parts = cid.split("-")
        assert len(parts) == 5  # UUID format

    def test_server_creates_wallet_for_new_customer(self):
        from gateway.server import create_app
        from gateway.wallet_sqlite import SQLiteWallet
        import tempfile, os
        
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        
        old_db = os.environ.get("TRYX402_DB_PATH")
        os.environ["TRYX402_DB_PATH"] = db_path
        
        try:
            from starlette.testclient import TestClient
            app = create_app()
            client = TestClient(app)
            
            # Mint a session, register a known origin, then hit the proxy
            sess = client.post("/v1/auth/session", json={}).json()
            app.state.price_registry.register(
                origin="https://example.com", price_cents=3)
            headers = {"X-Customer-ID": sess["customer_id"],
                       "X-Session-Token": sess["token"]}
            resp = client.post("/v1/proxy/call", json={
                "url": "https://example.com/api",
                "body": {"test": True},
            }, headers=headers)
            
            # Should get 402 (insufficient balance) not 401/404
            assert resp.status_code == 402
            
            # Wallet should have been created with 0 balance
            wallet = SQLiteWallet(db_path, sess["customer_id"])
            assert wallet.get_balance() == 0
        finally:
            if old_db is None:
                os.environ.pop("TRYX402_DB_PATH", None)
            else:
                os.environ["TRYX402_DB_PATH"] = old_db
            os.unlink(db_path)

    def test_server_blocks_without_customer_id(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        client = TestClient(app)
        resp = client.post("/v1/proxy/call", json={
            "url": "https://example.com/api",
            "body": {"test": True},
            "price_usd": 0.03,
        })
        assert resp.status_code == 401

    def test_gateway_proxy_call_sends_customer_id(self, monkeypatch):
        from gateway.api import Gateway
        from gateway.anon_auth import get_or_create_customer_id
        
        # Mock the HTTP response
        class MockResponse:
            def read(self):
                return json.dumps({
                    "status_code": 200,
                    "headers": {},
                    "body": "ok",
                    "cost_cents": 300,
                    "commission_cents": 30,
                    "total_cents": 330,
                    "new_balance_cents": 670,
                }).encode()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
        
        import urllib.request
        calls = []
        original_urlopen = urllib.request.urlopen
        
        def mock_urlopen(req, *a, **kw):
            calls.append(req)
            return MockResponse()
        
        monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
        
        gw = Gateway(api_key=None)  # No API key
        result = gw.proxy_call(
            "https://example.com/api",
            body={"test": True},
            price_usd=0.03,
        )
        
        assert len(calls) == 1
        req = calls[0]
        # HTTP headers are case-insensitive; urllib normalizes to lowercase
        headers_lower = {k.lower(): v for k, v in req.headers.items()}
        assert "x-customer-id" in headers_lower
        cid = headers_lower["x-customer-id"]
        assert len(cid) > 0
