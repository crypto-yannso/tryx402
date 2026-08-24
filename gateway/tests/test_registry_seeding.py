

# ---------------------------------------------------------------------------
# Registry seeding from the private tools DB (server-side pricing source)
# ---------------------------------------------------------------------------

import pytest

pytest.importorskip("fastapi")


class TestRegistrySeeding:
    def _make_tools_db(self, tmp_path, rows):
        """Mimic the private repo's tools table schema."""
        import sqlite3
        db = str(tmp_path / "tools.db")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE tools (
                id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL,
                origin TEXT NOT NULL, endpoint TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT 'POST',
                description TEXT DEFAULT '',
                price_usd REAL NOT NULL DEFAULT 0.01,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        for r in rows:
            conn.execute(
                "INSERT INTO tools (id, slug, origin, endpoint, method, price_usd, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r["id"], r["slug"], r["origin"], r["endpoint"],
                 r.get("method", "POST"), r["price_usd"],
                 1 if r.get("is_active", True) else 0))
        conn.commit()
        conn.close()
        return db

    def test_seed_from_private_tools_db(self, tmp_path):
        from gateway.registry import seed_from_tools_db
        tools_db = self._make_tools_db(tmp_path, [
            {"id": "1", "slug": "email-work", "origin": "https://a.example.com",
             "endpoint": "/api/email", "price_usd": 0.05},
            {"id": "2", "slug": "scrape", "origin": "https://b.example.com",
             "endpoint": "/api/scrape", "price_usd": 0.02, "is_active": False},
        ])
        reg = seed_from_tools_db(tools_db)
        # active tool priced at 5 cents; inactive tool must NOT be registered
        assert reg.lookup("https://a.example.com/api/email") == 5
        with pytest.raises(Exception):
            reg.lookup("https://b.example.com/api/scrape")

    def test_app_seeds_on_startup_when_env_set(self, monkeypatch, tmp_path):
        tools_db = self._make_tools_db(tmp_path, [
            {"id": "1", "slug": "email-work", "origin": "https://c.example.com",
             "endpoint": "/api/x", "price_usd": 0.10},
        ])
        monkeypatch.setenv("TRYX402_DB_PATH", str(tmp_path / "w.db"))
        monkeypatch.setenv("TRYX402_TOOLS_DB_PATH", tools_db)

        from gateway.server import create_app
        from starlette.testclient import TestClient
        app = create_app()
        client = TestClient(app)

        sess = client.post("/v1/auth/session", json={}).json()
        # No manual register() — pricing comes straight from the tools DB
        resp = client.post("/v1/proxy/call", headers={
            "X-Customer-ID": sess["customer_id"],
            "X-Session-Token": sess["token"],
        }, json={"url": "https://c.example.com/api/x", "method": "GET"})
        # 402 (empty wallet) proves the origin was accepted & priced server-side;
        # 400 would mean it was rejected as unknown.
        assert resp.status_code == 402, f"expected priced 402, got {resp.status_code}"
