"""Tests for dynamic checkout amount validation & tier handling."""

import pytest
from starlette.testclient import TestClient
from gateway.server import create_app


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


def test_amount_under_five_euros_rejected(client):
    resp = client.post("/v1/billing/checkout", json={
        "customer_email": "test@example.com",
        "amount_cents": 499,
        "currency": "eur"
    })
    assert resp.status_code == 422
    assert "Minimum top-up is 5.00 EUR" in resp.text


def test_amount_negative_rejected(client):
    resp = client.post("/v1/billing/checkout", json={
        "customer_email": "test@example.com",
        "amount_cents": -500,
        "currency": "eur"
    })
    assert resp.status_code == 422


def test_dynamic_amounts_accepted(monkeypatch, client):
    captured_calls = []

    class MockBilling:
        def __init__(self, amount_cents=None, currency=None):
            self.amount_cents = amount_cents
            self.currency = currency

        def create_checkout_session(self, email, mode="payment", metadata=None):
            captured_calls.append({
                "email": email,
                "amount_cents": self.amount_cents,
                "currency": self.currency,
                "metadata": metadata,
            })
            return {"url": f"https://checkout.stripe.com/cs_mock_{self.amount_cents}", "id": f"cs_{self.amount_cents}"}

    monkeypatch.setattr("gateway.server.StripeBilling", MockBilling)

    for cents in [500, 1250, 2500, 5000, 10000]:
        resp = client.post("/v1/billing/checkout", json={
            "customer_email": "user@example.com",
            "amount_cents": cents,
            "currency": "eur",
            "customer_id": "cust_123"
        })
        assert resp.status_code == 200
        assert resp.json()["session_id"] == f"cs_{cents}"

    assert len(captured_calls) == 5
    assert [c["amount_cents"] for c in captured_calls] == [500, 1250, 2500, 5000, 10000]
    assert captured_calls[0]["metadata"] == {"customer_id": "cust_123"}
