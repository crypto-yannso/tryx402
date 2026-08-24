# GET /api/v1/tools: public catalogue API backing the /tools web page.
# The page (gateway/site/tools.html) fetches this to render the visual
# catalogue. Seeds verified tools from seed_verified_tools.json on first run.
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")


class TestToolsCatalogAPI:
    def _client(self):
        from fastapi.testclient import TestClient
        from gateway.server import create_app

        return TestClient(create_app())

    def test_tools_endpoint_returns_success_shape(self):
        resp = self._client().get("/api/v1/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["count"] == len(data["tools"])
        assert isinstance(data["tools"], list)

    def test_tools_have_required_fields(self):
        data = self._client().get("/api/v1/tools").json()
        for t in data["tools"]:
            for field in ("slug", "origin", "endpoint", "price_usd"):
                assert field in t, f"missing {field}"

    def test_tools_endpoint_seeds_verified_tools(self, tmp_path, monkeypatch):
        # The catalogue must not be empty: seeded tools are the product.
        monkeypatch.setenv("TRYX402_TOOLS_DB_PATH", str(tmp_path / "tools.db"))
        data = self._client().get("/api/v1/tools").json()
        assert data["count"] > 0

    def test_active_only_filter(self):
        client = self._client()
        all_tools = client.get("/api/v1/tools?active_only=false").json()
        active = client.get("/api/v1/tools?active_only=true").json()
        assert all_tools["count"] >= active["count"]
