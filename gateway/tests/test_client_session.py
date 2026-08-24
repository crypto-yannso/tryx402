"""SDK client alignment with hardened server auth (session tokens).

The SDK must:
  - persist customer_id locally (anon_auth, unchanged behavior)
  - mint a session token from POST /v1/auth/session
  - send BOTH X-Customer-ID and X-Session-Token on every authenticated call
  - stop sending price_usd (server-side pricing now)

Run: python3 -m pytest gateway/tests/test_client_session.py -v
"""

import json

import pytest

pytest.importorskip("fastapi")


class MockResponse:
    def __init__(self, payload, code=200):
        self._payload = json.dumps(payload).encode()
        self.code = code

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _capture_urlopen(monkeypatch, responses=None):
    import urllib.request
    calls = []
    responses = responses if responses is not None else []

    def mock_urlopen(req, *a, **kw):
        calls.append(req)
        if responses:
            resp = responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp
        return MockResponse({})

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    return calls


class TestSessionAuth:
    def test_proxy_call_sends_customer_id_and_token(self, monkeypatch, tmp_path):
        from gateway import anon_auth
        monkeypatch.setattr(anon_auth, "CUSTOMER_ID_FILE", tmp_path / "cid.json")

        session_resp = MockResponse({"customer_id": "cid-abc", "token": "tok-xyz"})
        proxy_resp = MockResponse({
            "status_code": 200, "headers": {}, "body": "ok",
            "cost_cents": 300, "commission_cents": 50,
            "total_cents": 350, "new_balance_cents": 650,
        })
        calls = _capture_urlopen(monkeypatch, [session_resp, proxy_resp])

        from gateway.api import Gateway
        gw = Gateway(api_key=None)
        result = gw.proxy_call("https://known.example.com/api", body={"t": 1})

        assert len(calls) == 2
        # First call: session minting
        assert "/v1/auth/session" in calls[0].full_url
        # Second call: proxy with BOTH headers
        h = {k.lower(): v for k, v in calls[1].headers.items()}
        assert h.get("x-customer-id") == "cid-abc"
        assert h.get("x-session-token") == "tok-xyz"
        # No price_usd sent anymore — server decides the price
        body_sent = json.loads(calls[1].data.decode())
        assert "price_usd" not in body_sent
        assert result["total_cents"] == 350

    def test_token_is_cached_not_re_minted_per_call(self, monkeypatch, tmp_path):
        from gateway import anon_auth
        monkeypatch.setattr(anon_auth, "CUSTOMER_ID_FILE", tmp_path / "cid.json")

        session_resp = MockResponse({"customer_id": "cid-abc", "token": "tok-xyz"})
        proxy_resp = MockResponse({"status_code": 200, "headers": {}, "body": "ok",
                                   "cost_cents": 1, "commission_cents": 50,
                                   "total_cents": 51, "new_balance_cents": 949})
        calls = _capture_urlopen(monkeypatch, [session_resp, proxy_resp, proxy_resp])

        from gateway.api import Gateway
        gw = Gateway(api_key=None)
        gw.proxy_call("https://known.example.com/a", body={})
        gw.proxy_call("https://known.example.com/b", body={})

        session_calls = [c for c in calls if "/v1/auth/session" in c.full_url]
        assert len(session_calls) == 1, "token must be reused across calls"

    def test_balance_uses_session_headers(self, monkeypatch, tmp_path):
        from gateway import anon_auth
        monkeypatch.setattr(anon_auth, "CUSTOMER_ID_FILE", tmp_path / "cid.json")

        session_resp = MockResponse({"customer_id": "cid-abc", "token": "tok-xyz"})
        balance_resp = MockResponse({"customer_id": "cid-abc",
                                     "balance_cents": 500,
                                     "balance_display": "5.00 EUR"})
        calls = _capture_urlopen(monkeypatch, [session_resp, balance_resp])

        from gateway.api import Gateway
        gw = Gateway(api_key=None)
        bal = gw.check_balance()

        assert bal is not None and bal["balance_cents"] == 500
        h = {k.lower(): v for k, v in calls[1].headers.items()}
        assert h.get("x-session-token") == "tok-xyz"
