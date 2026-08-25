# Paid x402 responses must carry a signed Ed25519 receipt so the payer can
# prove what they paid for, offline, to any third party.
import base64
import json
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

PUBLIC_KEY = "0xabc0000000000000000000000000000000000001"
ORIGIN = "https://api.apify.com"
RECEIPT_SEED = "ab" * 32  # deterministic test key


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


class TestX402Receipts:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("TRYX402_PAY_TO_ADDRESS", PUBLIC_KEY)
        monkeypatch.setenv("TRYX402_RECEIPT_KEY", RECEIPT_SEED)
        monkeypatch.setenv("TRYX402_FACILITATOR_URL", "https://x402.org/facilitator")

    def _app_with_upstream(self, monkeypatch):
        from gateway.server import create_app
        import gateway.x402_payments as xp
        app = create_app()
        app.state.price_registry.register(origin=ORIGIN, price_cents=5)

        def fake_urlopen(req, timeout=15):
            url = req.full_url
            if "/verify" in url:
                return _FakeResponse({"isValid": True})
            if "/settle" in url:
                return _FakeResponse({"success": True})
            return _FakeResponse({"result": "ok"})

        monkeypatch.setattr(xp.urllib.request, "urlopen", fake_urlopen)
        return app

    def _post(self, client):
        payload = {"origin": ORIGIN, "path": "/run", "method": "POST",
                   "body": {}}
        return client.post("/v1/x402/call", json=payload,
                           headers={"X-PAYMENT": _payment_header()})

    def test_paid_response_includes_signed_receipt(self, monkeypatch):
        from starlette.testclient import TestClient
        from tryx402.receipts import ReceiptBuilder
        resp = self._post(TestClient(self._app_with_upstream(monkeypatch)))
        assert resp.status_code == 200
        data = resp.json()
        receipt = data.get("receipt")
        assert receipt is not None
        builder = ReceiptBuilder(seed=bytes.fromhex(RECEIPT_SEED))
        assert builder.verify(receipt) is True
        assert receipt["origin"] == ORIGIN
        # amount in USD matches the registry price (5 cents)
        assert abs(receipt["amount_usd"] - 0.05) < 1e-9
        # settlement tx recorded when the facilitator returns one; here the
        # fake settle returns {"success": True} so both may be absent
        assert "tx_hash" in receipt

    def test_receipt_header_present_on_paid_response(self, monkeypatch):
        from starlette.testclient import TestClient
        resp = self._post(TestClient(self._app_with_upstream(monkeypatch)))
        assert resp.headers.get("X-RECEIPT") == "1"

    def test_tampered_amount_fails_verification(self, monkeypatch):
        from starlette.testclient import TestClient
        from tryx402.receipts import ReceiptBuilder
        resp = self._post(TestClient(self._app_with_upstream(monkeypatch)))
        receipt = resp.json()["receipt"]
        receipt["amount_usd"] = 0.01  # tamper
        builder = ReceiptBuilder(seed=bytes.fromhex(RECEIPT_SEED))
        assert builder.verify(receipt) is False

    def test_no_receipt_key_configured_still_serves_but_unsigned(self, monkeypatch):
        # Absence of key must not break the paid path; receipt simply omitted
        # and flagged for ops (reconciliation relies on facilitator records).
        from starlette.testclient import TestClient
        import gateway.x402_payments as xp
        monkeypatch.delenv("TRYX402_RECEIPT_KEY", raising=False)
        xp._receipt_builder = None  # reset cached signer (test isolation)
        app = self._app_with_upstream(monkeypatch)
        resp = self._post(TestClient(app))
        assert resp.status_code == 200
        assert resp.json().get("receipt") is None
