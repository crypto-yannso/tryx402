# Tests for discovery & aggregator compliance (OpenAPI /openapi.json & x402scan discovery spec)
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")


class TestOpenAPIDiscovery:
    def test_openapi_contains_guidance_and_contact(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient

        app = create_app()
        client = TestClient(app)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()

        info = data.get("info", {})
        assert "x-guidance" in info
        assert "contact" in info
        assert "email" in info["contact"]

    def test_openapi_x402_call_route_has_payment_info_and_402_response(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient

        app = create_app()
        client = TestClient(app)
        resp = client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()

        paths = data.get("paths", {})
        assert "/v1/x402/call" in paths
        post_op = paths["/v1/x402/call"].get("post", {})

        # Must have responses.402
        assert "402" in post_op.get("responses", {})

        # Must have x-payment-info compliant with x402 discovery
        pinfo = post_op.get("x-payment-info", {})
        assert "price" in pinfo
        assert pinfo["price"].get("currency") == "USD"
        assert "protocols" in pinfo
        assert any("x402" in proto for proto in pinfo["protocols"])
