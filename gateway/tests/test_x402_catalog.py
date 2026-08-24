# GET /v1/x402/tools: public, no-auth discovery endpoint listing every
# registered wrapped tool with its x402 price. This is what aggregators
# (Bazaar, x402scan) and agents crawl to discover tryx402 services.
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")

PUBLIC_KEY = "0xabc0000000000000000000000000000000000001"


class TestX402ToolsCatalog:
    @pytest.fixture(autouse=True)
    def _pay_to_env(self, monkeypatch):
        # A tool without a configured settlement address is not sellable,
        # so listing requires TRYX402_PAY_TO_ADDRESS to be set.
        monkeypatch.setenv("TRYX402_PAY_TO_ADDRESS", PUBLIC_KEY)

    def _app(self):
        from gateway.server import create_app

        return create_app()

    def test_lists_registered_origins_with_atomic_prices(self):
        app = self._app()
        app.state.price_registry.register(
            origin="https://api.apify.com", price_cents=5)
        app.state.price_registry.register(
            origin="https://serpapi.com", price_cents=10)
        from starlette.testclient import TestClient
        resp = TestClient(app).get("/v1/x402/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data["x402Version"] == 1
        tools = {t["origin"]: t for t in data["tools"]}
        assert tools["https://api.apify.com"]["maxAmountRequired"] == 50_000
        assert tools["https://serpapi.com"]["maxAmountRequired"] == 100_000
        assert tools["https://api.apify.com"]["scheme"] == "exact"

    def test_empty_registry_returns_empty_tools_not_error(self):
        from starlette.testclient import TestClient
        resp = TestClient(self._app()).get("/v1/x402/tools")
        assert resp.status_code == 200
        assert resp.json()["tools"] == []

    def test_each_tool_carries_resource_url_for_payment_flow(self):
        app = self._app()
        app.state.price_registry.register(
            origin="https://api.apify.com", price_cents=5)
        from starlette.testclient import TestClient
        data = TestClient(app).get("/v1/x402/tools").json()
        tool = data["tools"][0]
        # The agent must be able to POST to the facade resource and get a 402;
        # the sold origin is identified in the description, not in the URL.
        assert tool["resource"].endswith("/v1/x402/call")
        assert tool["origin"] == "https://api.apify.com"
        assert tool["payTo"]  # settlement address visible for facilitators

    def test_private_origins_are_never_listed(self):
        app = self._app()
        app.state.price_registry.register(
            origin="http://localhost:8080", price_cents=5, allow_private=True)
        from starlette.testclient import TestClient
        data = TestClient(app).get("/v1/x402/tools").json()
        origins = [t["origin"] for t in data["tools"]]
        assert "http://localhost:8080" not in origins
