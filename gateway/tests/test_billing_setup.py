"""Tests for /v1/billing/setup endpoint (auto-create Stripe product/price).

Run: python3 -m pytest gateway/tests/test_billing_setup.py -v
"""

import json
import pytest


class TestBillingSetup:
    """Tests for automatic Stripe product/price creation."""

    def test_setup_requires_stripe_key(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        client = TestClient(app)
        resp = client.post("/v1/billing/setup", json={
            "product_name": "tryx402 credit",
            "price_cents": 1000,
            "currency": "eur",
        })
        # Should fail without Stripe key (501 = not configured)
        assert resp.status_code in (400, 500, 501)

    def test_setup_creates_product_and_price(self, monkeypatch):
        from gateway.billing import StripeBilling
        from gateway.server import create_app
        from starlette.testclient import TestClient
        
        # Mock StripeBilling
        class MockBilling:
            def create_product_and_price(self, product_name, price_cents, currency):
                return {
                    "product_id": "prod_test123",
                    "price_id": "price_test456",
                    "product_name": product_name,
                    "price_cents": price_cents,
                    "currency": currency,
                }
        
        monkeypatch.setattr("gateway.server.StripeBilling", lambda **kw: MockBilling())
        
        app = create_app()
        client = TestClient(app)
        resp = client.post("/v1/billing/setup", json={
            "product_name": "tryx402 usage credit",
            "price_cents": 1000,
            "currency": "eur",
        })
        
        assert resp.status_code == 200
        data = resp.json()
        assert data["product_id"] == "prod_test123"
        assert data["price_id"] == "price_test456"
        assert data["price_cents"] == 1000

    def test_setup_validates_price_cents(self, monkeypatch):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        
        app = create_app()
        client = TestClient(app)
        
        # Negative price
        resp = client.post("/v1/billing/setup", json={
            "product_name": "test",
            "price_cents": -100,
            "currency": "eur",
        })
        assert resp.status_code == 422  # Validation error
        
        # Zero price
        resp = client.post("/v1/billing/setup", json={
            "product_name": "test",
            "price_cents": 0,
            "currency": "eur",
        })
        assert resp.status_code == 422
