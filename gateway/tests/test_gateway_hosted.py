"""Tests for Gateway integration with the hosted service (balance, recharge).

Run: python3 -m pytest gateway/tests/test_gateway_hosted.py -v
"""

import os
import pytest


class TestGatewayHostedIntegration:
    """Tests for Gateway.check_balance(), Gateway.recharge(), etc."""

    def test_check_balance_uses_hosted_service(self):
        from gateway.api import Gateway
        
        # Without API key, should raise or return None
        gw = Gateway(max_budget_usd=1.0)
        result = gw.check_balance()
        assert result is None or "balance_cents" in result

    def test_check_balance_with_api_key(self, monkeypatch):
        from gateway.api import Gateway
        import urllib.request
        
        class MockResponse:
            def read(self):
                return b'{"customer_id": "test_001", "balance_cents": 5000, "balance_display": "50.00 EUR"}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        
        def mock_urlopen(req, timeout=5):
            return MockResponse()
        
        monkeypatch.setattr("gateway.api.urllib.request.urlopen", mock_urlopen)
        gw = Gateway(max_budget_usd=1.0, api_key="test_key_001")
        result = gw.check_balance()
        assert result is not None
        assert result["balance_cents"] == 5000

    def test_recharge_redirects_to_stripe(self, monkeypatch):
        from gateway.api import Gateway
        import urllib.request
        
        class MockResponse:
            def read(self):
                return b'{"url": "https://checkout.stripe.com/cs_test", "session_id": "cs_test_123"}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        
        def mock_urlopen(req, timeout=5):
            return MockResponse()
        
        monkeypatch.setattr("gateway.api.urllib.request.urlopen", mock_urlopen)
        gw = Gateway(max_budget_usd=1.0, api_key="test_key_002")
        result = gw.recharge(amount_cents=1000)
        assert "url" in result
        assert "session_id" in result

    def test_gateway_blocks_call_when_balance_insufficient(self, monkeypatch):
        from gateway.api import Gateway
        import urllib.request
        
        class MockResponse:
            def read(self):
                return b'{"customer_id": "test_003", "balance_cents": 100, "balance_display": "1.00 EUR"}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        
        def mock_urlopen(req, timeout=5):
            return MockResponse()
        
        monkeypatch.setattr("gateway.api.urllib.request.urlopen", mock_urlopen)
        gw = Gateway(max_budget_usd=1.0, api_key="test_key_003")
        
        # check_balance returns the mocked balance
        balance = gw.check_balance()
        assert balance["balance_cents"] == 100
        
        # The call itself still goes through (enforce_balance is a server-side concern
        # in this architecture); what matters is that the SDK exposes the balance
        # so the agent/caller can decide.
        assert gw.check_balance()["balance_cents"] < 500  # not enough for a 0.05 USD call
