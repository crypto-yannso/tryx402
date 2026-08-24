# GET /v1/x402/listing + /v1/x402/bazaar.json: public syndication endpoints.
# They expose the registry as a generic x402 resources document and as a
# Bazaar-style flat feed so aggregators (Onyx Bazaar, gold-402, x402scan)
# can index tryx402-wrapped endpoints without any bespoke integration.
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

PUBLIC_KEY = "0xabc0000000000000000000000000000000000001"


class TestX402Listing:
    @pytest.fixture(autouse=True)
    def _pay_to_env(self, monkeypatch):
        monkeypatch.setenv("TRYX402_PAY_TO_ADDRESS", PUBLIC_KEY)

    def _app(self):
        from gateway.server import create_app

        return create_app()

    def test_listing_returns_x402_resources_document(self):
        from fastapi.testclient import TestClient

        client = TestClient(self._app())
        resp = client.get("/v1/x402/listing")
        assert resp.status_code == 200
        doc = resp.json()
        assert doc["x402Version"] == 1
        assert isinstance(doc["resources"], list)

    def test_listing_requires_pay_to(self, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.delenv("TRYX402_PAY_TO_ADDRESS", raising=False)
        client = TestClient(self._app())
        resp = client.get("/v1/x402/listing")
        assert resp.status_code == 503
        assert "pay_to" in resp.json()["detail"].lower()

    def test_bazaar_feed_is_flat_list_with_required_fields(self):
        from fastapi.testclient import TestClient

        client = TestClient(self._app())
        resp = client.get("/v1/x402/bazaar.json")
        assert resp.status_code == 200
        feed = resp.json()
        assert isinstance(feed, list)
        for item in feed:
            for field in ("id", "endpoint", "price", "scheme",
                          "network", "asset", "payTo", "resource"):
                assert field in item

    def test_listing_matches_catalog_prices(self):
        # The syndicated prices must be exactly what /v1/x402/call charges:
        # one source of truth, two representations.
        from fastapi.testclient import TestClient

        client = TestClient(self._app())
        tools = client.get("/v1/x402/tools").json()["tools"]
        listing = client.get("/v1/x402/listing").json()["resources"]
        by_origin_tools = {t["origin"]: t["maxAmountRequired"] for t in tools}
        by_origin_listing = {r["origin"]: r["maxAmountRequired"] for r in listing}
        assert by_origin_tools == by_origin_listing
