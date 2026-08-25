import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")


class TestStaticAndRootAssets:
    def test_root_serves_html(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient

        app = create_app()
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    def test_favicon_ico_exists(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient

        app = create_app()
        client = TestClient(app)
        resp = client.get("/favicon.ico")
        assert resp.status_code == 200
        assert resp.headers.get("content-type") == "image/x-icon"

    def test_favicon_svg_exists(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient

        app = create_app()
        client = TestClient(app)
        resp = client.get("/favicon.svg")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers.get("content-type", "")

    def test_favicon_png_sizes_exist(self):
        from gateway.server import create_app
        from starlette.testclient import TestClient

        app = create_app()
        client = TestClient(app)
        for size in ["16", "32", "48", "512"]:
            resp = client.get(f"/favicon-{size}.png")
            assert resp.status_code == 200
            assert resp.headers.get("content-type") == "image/png"
