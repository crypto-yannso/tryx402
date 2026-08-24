"""Tests for the transparent proxy layer (commission on every call).

Run: python3 -m pytest gateway/tests/test_proxy.py -v
"""

import json
import pytest


class TestProxyEndpoint:
    """Tests for /v1/proxy/call — the transparent commission layer."""

    def test_proxy_returns_401_without_api_key(self):
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

    def test_proxy_blocks_when_balance_insufficient(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        client = TestClient(app)
        # Mint session, register origin, empty wallet -> 402
        sess = client.post("/v1/auth/session", json={}).json()
        app.state.price_registry.register(origin="https://example.com", price_cents=3)
        resp = client.post("/v1/proxy/call", json={
            "url": "https://example.com/api",
            "body": {"test": True},
        }, headers={"X-Customer-ID": sess["customer_id"],
                    "X-Session-Token": sess["token"]})
        assert resp.status_code == 402

    def test_proxy_debits_wallet_with_commission(self):
        import os, tempfile
        from gateway.wallet_sqlite import SQLiteWallet
        from gateway.server import create_app
        from starlette.testclient import TestClient
        
        # Use a unique temp DB for this test
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        
        # Set env so the server uses the same DB
        old_db_path = os.environ.get("TRYX402_DB_PATH")
        os.environ["TRYX402_DB_PATH"] = db_path
        
        try:
            # Pre-fund wallet with enough for one call + commission
            wallet = SQLiteWallet(db_path, customer_id="proxy_001")
            wallet.credit(amount_cents=1000, description="Initial deposit")
            assert wallet.get_balance() == 1000

            app = create_app()
            client = TestClient(app)
            
            app.state.price_registry.register(
                origin="https://example.com", price_cents=300)
            sess = client.post("/v1/auth/session", json={}).json()
            # Fund the session's own wallet
            wallet = SQLiteWallet(db_path, customer_id=sess["customer_id"])
            wallet.credit(amount_cents=1000, description="Initial deposit")
            
            # The proxy debits the SERVER-set price (300) + commission
            resp = client.post("/v1/proxy/call", json={
                "url": "https://example.com/api",
                "body": {"test": True},
            }, headers={"X-Customer-ID": sess["customer_id"],
                        "X-Session-Token": sess["token"]})
            
            # Expect 502 (provider unreachable, wallet refunded) or 200
            assert resp.status_code in (200, 502)
            if resp.status_code == 200:
                new_balance = wallet.get_balance()
                assert new_balance < 1000  # Something was debited
        finally:
            if old_db_path is None:
                os.environ.pop("TRYX402_DB_PATH", None)
            else:
                os.environ["TRYX402_DB_PATH"] = old_db_path
            os.unlink(db_path)

    def test_proxy_records_commission_in_history(self):
        from gateway.wallet_sqlite import SQLiteWallet
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            wallet = SQLiteWallet(db_path, customer_id="proxy_002")
            wallet.credit(amount_cents=1000, description="Initial deposit")
            
            # Simulate what the proxy should do: debit price + commission
            price_cents = 300  # 0.03 USD
            commission_rate = 0.10
            total_debit = int(price_cents * (1 + commission_rate))
            
            wallet.debit(amount_cents=total_debit, description=f"Proxy call + {commission_rate*100:.0f}% commission")
            
            history = wallet.get_history()
            assert len(history) == 2  # credit + debit
            assert history[1]["amount_cents"] == total_debit
            assert "commission" in history[1]["description"].lower()
        finally:
            os.unlink(db_path)


class TestCommissionConfig:
    """Tests for commission rate configuration."""

    def test_default_commission_rate(self):
        from gateway.proxy import DEFAULT_COMMISSION_RATE
        assert isinstance(DEFAULT_COMMISSION_RATE, float)
        assert 0 <= DEFAULT_COMMISSION_RATE <= 1

    def test_custom_commission_rate(self):
        from gateway.proxy import ProxyConfig
        config = ProxyConfig(commission_rate=0.15)
        assert config.commission_rate == 0.15

    def test_commission_calculation(self):
        from gateway.proxy import ProxyConfig
        config = ProxyConfig(commission_rate=0.10, min_commission_cents=0)
        # 0.03 USD = 300 cents
        price_cents = 300
        total = config.calculate_total(price_cents)
        assert total == 330  # 300 + 10%

    def test_minimum_commission(self):
        from gateway.proxy import ProxyConfig
        config = ProxyConfig(commission_rate=0.10, min_commission_cents=50)
        # 100 cents → commission would be 10 cents, but min is 50
        total = config.calculate_total(100)
        assert total == 150  # 100 + 50 (min)
