# POST /v1/x402/call: the pay-per-call facade endpoint. Without a valid
# X-PAYMENT header it must return a spec-conformant HTTP 402 carrying the
# payment requirements. This is the endpoint agents actually hit.
import base64
import json
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

PUBLIC_KEY = "0xabc0000000000000000000000000000000000001"
ORIGIN = "https://api.apify.com"
PRICE_CENTS = 5


class TestX402CallPaywall:
    @pytest.fixture(autouse=True)
    def _pay_to_env(self, monkeypatch):
        monkeypatch.setenv("TRYX402_PAY_TO_ADDRESS", PUBLIC_KEY)

    def _register(self, app):
        app.state.price_registry.register(origin=ORIGIN, price_cents=PRICE_CENTS)
        return app

    def _call(self, client, headers=None, body=None):
        payload = {"origin": ORIGIN, "path": "/v2/acts/apify~web-scraper/run-sync",
                   "method": "POST", "body": body or {}}
        return client.post("/v1/x402/call", json=payload, headers=headers or {})

    def test_no_payment_header_returns_402(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = self._register(create_app())
        resp = self._call(TestClient(app))
        assert resp.status_code == 402

    def test_402_body_is_x402_conformant(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = self._register(create_app())
        resp = self._call(TestClient(app))
        data = resp.json()
        assert data["x402Version"] == 1
        accepts = data["accepts"]
        assert accepts[0]["scheme"] == "exact"
        assert accepts[0]["maxAmountRequired"] == PRICE_CENTS * 10_000  # atomic USDC
        assert accepts[0]["payTo"] == PUBLIC_KEY

    def test_unknown_origin_returns_400_not_402(self):
        # An origin not in the registry is never sellable — that's a client
        # error (bad request), not a payment request.
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        payload = {"origin": "https://unregistered.example.com", "path": "/x"}
        resp = TestClient(app).post("/v1/x402/call", json=payload)
        assert resp.status_code == 400

    def test_private_origin_blocked_even_with_payment(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        app.state.price_registry.register(
            origin="http://169.254.169.254", price_cents=1)  # SSRF attempt target
        payload = {"origin": "http://169.254.169.254", "path": "/latest/meta-data"}
        resp = TestClient(app).post("/v1/x402/call", json=payload)
        assert resp.status_code == 403
