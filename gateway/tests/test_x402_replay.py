# Replay protection: the same X-PAYMENT header must never be processed twice.
# A facilitator verify might pass again on replay; tryx402 must reject it
# server-side (the double-charge lesson).
import base64
import hashlib
import json
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

PUBLIC_KEY = "0xabc0000000000000000000000000000000000001"
ORIGIN = "https://api.apify.com"


def _payment_header(nonce="nonce-1"):
    payload = {"x402Version": 1, "scheme": "exact", "from": "0xpayer01",
               "nonce": nonce}
    return base64.b64encode(json.dumps(payload).encode()).decode()


class _FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestX402ReplayProtection:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("TRYX402_PAY_TO_ADDRESS", PUBLIC_KEY)
        monkeypatch.setenv("TRYX402_FACILITATOR_URL", "https://x402.org/facilitator")

    def test_same_payment_header_replayed_is_rejected(self, monkeypatch):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        import gateway.x402_payments as xp

        settle_calls = []

        def fake_urlopen(req, timeout=15):
            url = req.full_url
            if "/verify" in url:
                return _FakeResponse({"isValid": True})
            if "/settle" in url:
                settle_calls.append(1)
                return _FakeResponse({"success": True})
            return _FakeResponse({"result": "ok"})

        monkeypatch.setattr(xp.urllib.request, "urlopen", fake_urlopen)
        app = create_app()
        app.state.price_registry.register(origin=ORIGIN, price_cents=5)
        client = TestClient(app)

        payload = {"origin": ORIGIN, "path": "/run", "method": "POST", "body": {}}
        headers = {"X-PAYMENT": _payment_header()}
        r1 = client.post("/v1/x402/call", json=payload, headers=headers)
        assert r1.status_code == 200
        # REPLAY: same exact header
        r2 = client.post("/v1/x402/call", json=payload, headers=headers)
        assert r2.status_code == 409  # conflict: duplicate payment
        assert len(settle_calls) == 1  # settled exactly once

    def test_different_payments_are_not_collided(self, monkeypatch):
        from gateway.server import create_app
        from starlette.testclient import TestClient
        import gateway.x402_payments as xp

        def fake_urlopen(req, timeout=15):
            url = req.full_url
            if "/verify" in url:
                return _FakeResponse({"isValid": True})
            if "/settle" in url:
                return _FakeResponse({"success": True})
            return _FakeResponse({"result": "ok"})

        monkeypatch.setattr(xp.urllib.request, "urlopen", fake_urlopen)
        app = create_app()
        app.state.price_registry.register(origin=ORIGIN, price_cents=5)
        client = TestClient(app)
        payload = {"origin": ORIGIN, "path": "/run", "method": "POST", "body": {}}
        r1 = client.post("/v1/x402/call", json=payload,
                         headers={"X-PAYMENT": _payment_header("a")})
        r2 = client.post("/v1/x402/call", json=payload,
                         headers={"X-PAYMENT": _payment_header("b")})
        assert r1.status_code == 200 and r2.status_code == 200

    def test_failed_upstream_does_not_consume_the_payment_slot(self, monkeypatch):
        # If upstream failed after payment, a retry with THE SAME header is
        # still a paid attempt that reached verify — it must be flagged as a
        # deliberate manual retry (409), not silently dropped. The payer gets
        # reconciliation instructions instead of a silent second charge path.
        from gateway.server import create_app
        from starlette.testclient import TestClient
        import gateway.x402_payments as xp

        state = {"upstream_fail": True}

        def fake_urlopen(req, timeout=15):
            url = req.full_url
            if "/verify" in url:
                return _FakeResponse({"isValid": True})
            if "/settle" in url:
                return _FakeResponse({"success": True})
            if state["upstream_fail"]:
                raise OSError("connection reset")
            return _FakeResponse({"result": "ok"})

        monkeypatch.setattr(xp.urllib.request, "urlopen", fake_urlopen)
        app = create_app()
        app.state.price_registry.register(origin=ORIGIN, price_cents=5)
        client = TestClient(app)
        payload = {"origin": ORIGIN, "path": "/run", "method": "POST", "body": {}}
        headers = {"X-PAYMENT": _payment_header(nonce="upstream-fail-1")}
        r1 = client.post("/v1/x402/call", json=payload, headers=headers)
        assert r1.status_code == 502 and r1.json()["detail"]["reconciliation_required"]
        # same header retried -> still blocked (manual resolution required)
        state["upstream_fail"] = False
        r2 = client.post("/v1/x402/call", json=payload, headers=headers)
        assert r2.status_code == 409