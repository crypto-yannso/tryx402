# GET /tools and /provider: visual web pages proxied from www.tryx402.app.
# The nav of the public site links here via vercel.json rewrites; these pages
# must exist on the Fly deployment or the nav 404s. TDD: tests first.
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")


class TestSitePages:
    def _app(self):
        from gateway.server import create_app

        return create_app()

    def _client(self):
        from fastapi.testclient import TestClient

        return TestClient(self._app())

    def test_tools_page_served(self):
        resp = self._client().get("/tools")
        assert resp.status_code == 200
        assert "html" in resp.headers.get("content-type", "")

    def test_provider_page_served(self):
        resp = self._client().get("/provider")
        assert resp.status_code == 200
        assert "html" in resp.headers.get("content-type", "")

    def test_french_variants_served(self):
        client = self._client()
        for path in ("/tools", "/provider"):
            resp = client.get(path, headers={"Accept-Language": "fr"})
            assert resp.status_code == 200

    def test_unknown_page_still_404(self):
        assert self._client().get("/definitely-not-a-page").status_code == 404
