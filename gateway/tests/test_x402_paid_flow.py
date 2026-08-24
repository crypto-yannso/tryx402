# The paid path: X-PAYMENT present -> decode -> verify via facilitator ->
# forward to provider -> settle. Facilitator is faked (monkeypatched urllib)
# so tests never touch a chain.
import base64
import json
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

PUBLIC_KEY = "0xabc0000000000000000000000000000000000001"
ORIGIN = "https://api.apify.com"


def _payment_header(payer="0xpayer0000000000000000000000000000000001"):
    payload = {"x402Version": 1, "scheme": "exact", "from": payer}
    return base64.b64encode(json.dumps(payload).encode()).decode()


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = json.dumps(body).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestX402PaidCall:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("TRYX402_PAY_TO_ADDRESS", PUBLIC_KEY)

    def _register(self, app):
        app.state.price_registry.register(origin=ORIGIN, price_cents=5)
        return app

    def _post(self, client, headers=None, upstream=None):
        payload = {"origin": ORIGIN, "path": "/run", "method": "POST",
                   "body": {"q": "test"}}
        return client.post("/v1/x402/call", json=payload,
                           headers=headers or {"X-PAYMENT": _payment_header()})

    def test_valid_payment_proxies_and_settles(self, monkeypatch):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        import gateway.x402_payments as xp

        calls = []

        def fake_urlopen(req, timeout=15):
            url = req.full_url if hasattr(req, "full_url") else req
            calls.append(url)
            if "/verify" in str(url):
                return _FakeResponse({"isValid": True})
            if "/settle" in str(url):
                return _FakeResponse({"success": True})
            # upstream provider call
            return _FakeResponse({"result": "scraped-data"})

        monkeypatch.setattr(xp.urllib.request, "urlopen", fake_urlopen)
        app = self._register(create_app())
        resp = self._post(TestClient(app))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status_code"] == 200
        assert json.loads(data["body"])["result"] == "scraped-data"
        assert data["settlement"]["success"] is True
        # exactly one verify and one settle, no more (no-retry discipline)
        assert sum(1 for c in calls if "/verify" in c) == 1
        assert sum(1 for c in calls if "/settle" in c) == 1


    def test_facilitator_wire_format_uses_payment_payload_key(self, monkeypatch):
        # x402.org facilitator expects "paymentPayload", not "paymentHeader".
        import gateway.x402_payments as xp

        captured = {}

        def fake_urlopen(req, timeout=15):
            captured["body"] = json.loads(req.data.decode())
            if "/verify" in req.full_url:
                return _FakeResponse({"isValid": True})
            if "/settle" in req.full_url:
                return _FakeResponse({"success": True})
            return _FakeResponse({"result": "ok"})

        monkeypatch.setattr(xp.urllib.request, "urlopen", fake_urlopen)
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        app.state.price_registry.register(origin=ORIGIN, price_cents=5)
        payload = {"origin": ORIGIN, "path": "/run", "method": "POST", "body": {}}
        resp = TestClient(app).post("/v1/x402/call", json=payload,
                                    headers={"X-PAYMENT": _payment_header()})
        assert resp.status_code == 200
        sent_verify = captured["body"]
        assert "paymentPayload" in sent_verify
        assert "paymentRequirements" in sent_verify

    def test_invalid_payment_rejected_with_402(self, monkeypatch):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        import gateway.x402_payments as xp

        def fake_urlopen(req, timeout=15):
            if "/verify" in req.full_url:
                return _FakeResponse({"isValid": False,
                                      "invalidReason": "insufficient_funds"})
            raise AssertionError("must not reach settle or upstream")

        monkeypatch.setattr(xp.urllib.request, "urlopen", fake_urlopen)
        app = self._register(create_app())
        resp = self._post(TestClient(app))
        assert resp.status_code == 402
        assert "insufficient_funds" in resp.text

    def test_malformed_payment_header_is_400(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = self._register(create_app())
        resp = self._post(TestClient(app),
                          headers={"X-PAYMENT": "not-base64-json!!!"})
        assert resp.status_code == 400

    def test_upstream_failure_flags_reconciliation_never_retries(self, monkeypatch):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        import gateway.x402_payments as xp

        attempts = []

        def fake_urlopen(req, timeout=15):
            url = req.full_url
            if "/verify" in url:
                return _FakeResponse({"isValid": True})
            if "/settle" in url:
                raise AssertionError("settle must not happen after upstream failure")
            attempts.append(1)
            raise OSError("connection reset")

        monkeypatch.setattr(xp.urllib.request, "urlopen", fake_urlopen)
        app = self._register(create_app())
        resp = self._post(TestClient(app))
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert detail["reconciliation_required"] is True
        assert len(attempts) == 1  # exactly one attempt, never retried
