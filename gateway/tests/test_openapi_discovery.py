# Test that openapi is intentionally disabled on the raw Fly backend (served via www.tryx402.app only)
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")


class TestOpenAPIDiscovery:
    def test_openapi_disabled_on_backend_to_prevent_duplicate_origins(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient

        app = create_app()
        client = TestClient(app)
        resp = client.get("/openapi.json")
        assert resp.status_code == 404

