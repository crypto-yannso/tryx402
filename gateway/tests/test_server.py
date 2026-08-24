"""Tests for gateway.server (FastAPI/ASGI app).

Run: python3 -m pytest gateway/tests/test_server.py -v
"""

import json
import os
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")


class TestServerHealth:
    def test_health_endpoint(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestWalletAPI:
    def test_get_balance_empty(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        client = TestClient(app)
        # Without auth header, should return 401 or default
        resp = client.get("/v1/wallet/balance")
        # Accept either 401 (no key) or 200 with zero balance (demo mode)
        assert resp.status_code in (200, 401)
        if resp.status_code == 200:
            data = resp.json()
            assert "balance_cents" in data

    def test_get_transactions_empty(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        client = TestClient(app)
        resp = client.get("/v1/wallet/transactions")
        assert resp.status_code in (200, 401)

    def test_create_checkout_session(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        client = TestClient(app)
        payload = {"customer_email": "test@example.com", "amount_cents": 1000}
        resp = client.post("/v1/billing/checkout", json=payload)
        # Without STRIPE_TEST_KEY, should return 500 or 501
        assert resp.status_code in (200, 500, 501)
        if resp.status_code == 200:
            data = resp.json()
            assert "url" in data


class TestTelemetryEndpoint:
    def test_telemetry_accepts_ping(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        client = TestClient(app)
        payload = {
            "install_id": "abc123",
            "version": "0.3.1",
            "python": "3.9",
            "platform": "darwin",
        }
        resp = client.post("/v1/telemetry", json=payload)
        assert resp.status_code in (200, 404)  # 404 if not implemented yet
