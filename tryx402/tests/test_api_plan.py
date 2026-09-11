"""Deterministic tests for unpaid live pricing in ``Gateway.plan``."""
from __future__ import annotations

import base64
import io
import json
import urllib.error
from unittest.mock import patch

from tryx402.api import Gateway


SEEDANCE_URL = "https://stablestudio.dev/api/generate/seedance/t2v"
SEEDANCE_BODY = {
    "prompt": "pricing preflight only",
    "duration": "10",
    "aspectRatio": "16:9",
    "outputResolution": "720p",
}


def _catalog_at_20(_path):
    return {"tools": [{"full_url": SEEDANCE_URL, "price_usd": 20.0}]}


def _http_error(*, headers=None, body=b"{}", code=402):
    return urllib.error.HTTPError(
        SEEDANCE_URL,
        code,
        "Payment Required" if code == 402 else "error",
        headers or {},
        io.BytesIO(body),
    )


class _Response:
    def __init__(self, code=200, headers=None, body=b"{}"):
        self.status = code
        self.headers = headers or {}
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_plan_prefers_live_402_quote_over_stale_catalog():
    challenge = {
        "x402Version": 2,
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": "USDC",
            "amount": "2400000",
            "extra": {"name": "USD Coin", "version": "2", "decimals": 6},
        }],
    }
    encoded = base64.b64encode(json.dumps(challenge).encode()).decode()
    error = _http_error(
        headers={"PAYMENT-REQUIRED": encoded},
        body=b'{"checkout":{"total":{"amount":"2.40","currency":"USD"}}}',
    )
    gateway = Gateway(max_budget_usd=5.0)
    gateway._http_get = _catalog_at_20

    with patch.object(gateway, "_open_quote_request", side_effect=error) as urlopen:
        plan = gateway.plan([{
            "url": SEEDANCE_URL,
            "method": "POST",
            "body": SEEDANCE_BODY,
        }])

    assert urlopen.call_count == 1
    request = urlopen.call_args.args[0]
    assert request.get_method() == "POST"
    assert json.loads(request.data) == SEEDANCE_BODY
    assert "X-PAYMENT" not in request.headers
    assert "PAYMENT-SIGNATURE" not in request.headers
    assert plan["total_usd"] == 2.4
    assert plan["fits_budget"] is True
    assert plan["steps"][0]["price_usd"] == 2.4
    assert plan["steps"][0]["price_source"] == "live_402"


def test_plan_uses_declared_asset_decimals():
    challenge = {
        "x402Version": 2,
        "accepts": [{
            "amount": "240000000",
            "asset": "TEST",
            "extra": {"decimals": 8},
        }],
    }
    encoded = base64.urlsafe_b64encode(json.dumps(challenge).encode()).decode().rstrip("=")
    gateway = Gateway()
    gateway._http_get = lambda _path: {"tools": []}

    with patch.object(
        gateway,
        "_open_quote_request",
        side_effect=_http_error(headers={"payment-required": encoded}),
    ):
        plan = gateway.plan([{"url": SEEDANCE_URL, "method": "POST", "body": SEEDANCE_BODY}])

    assert plan["steps"][0]["price_usd"] == 2.4
    assert plan["steps"][0]["price_source"] == "live_402"


def test_plan_falls_back_to_catalog_for_malformed_challenge():
    gateway = Gateway()
    gateway._http_get = _catalog_at_20
    error = _http_error(headers={"PAYMENT-REQUIRED": "not-base64"}, body=b"not-json")

    with patch.object(gateway, "_open_quote_request", side_effect=error):
        plan = gateway.plan([{"url": SEEDANCE_URL, "method": "POST", "body": SEEDANCE_BODY}])

    assert plan["steps"][0]["price_usd"] == 20.0
    assert plan["steps"][0]["price_source"] == "catalog"
    assert plan["steps"][0]["quote_status"] == "live_402_unparseable"


def test_plan_falls_back_for_missing_challenge():
    gateway = Gateway()
    gateway._http_get = _catalog_at_20

    with patch.object(gateway, "_open_quote_request", side_effect=_http_error()):
        plan = gateway.plan([{"url": SEEDANCE_URL, "body": SEEDANCE_BODY}])

    assert plan["steps"][0]["price_usd"] == 20.0
    assert plan["steps"][0]["price_source"] == "catalog"
    assert plan["steps"][0]["quote_status"] == "live_402_unparseable"


def test_plan_reads_ucp_checkout_total_from_402_body():
    gateway = Gateway()
    gateway._http_get = _catalog_at_20
    error = _http_error(body=b'{"checkout":{"total":{"amount":"2.40","currency":"USD"}}}')

    with patch.object(gateway, "_open_quote_request", side_effect=error):
        plan = gateway.plan([{"url": SEEDANCE_URL, "method": "POST", "body": SEEDANCE_BODY}])

    assert plan["steps"][0]["price_usd"] == 2.4
    assert plan["steps"][0]["price_source"] == "live_402"


def test_plan_never_retries_or_treats_2xx_as_a_quote():
    gateway = Gateway()
    gateway._http_get = _catalog_at_20

    with patch.object(gateway, "_open_quote_request", return_value=_Response(200)) as opener:
        plan = gateway.plan([{"url": SEEDANCE_URL, "method": "POST", "body": SEEDANCE_BODY}])

    assert opener.call_count == 1
    request = opener.call_args.args[0]
    assert all("payment" not in key.lower() for key in request.headers)
    assert request.headers["X-tryx402-preflight"] == "quote-only"
    assert plan["steps"][0]["price_source"] == "catalog"
    assert plan["steps"][0]["quote_status"] == "non_402_response_ignored"


def test_plan_preserves_intentionally_explicit_price_without_preflight():
    gateway = Gateway()
    gateway._http_get = _catalog_at_20

    with patch.object(gateway, "_open_quote_request") as opener:
        plan = gateway.plan([{"url": SEEDANCE_URL, "price": 0}])

    opener.assert_not_called()
    assert plan["steps"][0]["price_usd"] == 0.0
    assert plan["steps"][0]["price_source"] == "explicit"


def test_plan_uses_get_parameters_in_the_preflight_query():
    gateway = Gateway()
    gateway._http_get = lambda _path: {"tools": []}

    with patch.object(gateway, "_open_quote_request", side_effect=_http_error()) as opener:
        gateway.plan([{
            "url": SEEDANCE_URL,
            "method": "GET",
            "parameters": {"duration": "10", "tag": ["a", "b"]},
        }])

    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.full_url.endswith("duration=10&tag=a&tag=b")
