"""Sessions must survive process restarts (Fly.io scale-to-zero!) and the
SDK must recover transparently when its cached token becomes invalid.

Run: python3 -m pytest gateway/tests/test_session_persistence.py -v
"""

import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette")
pytest.importorskip("httpx")


class TestPersistentSessionStore:
    def test_token_survives_store_recreation(self, tmp_path):
        from gateway.sessions import SessionStore
        db = str(tmp_path / "s.db")

        s1 = SessionStore(db)
        cid, tok = s1.create()
        assert s1.verify(cid, tok)

        # Simulate a process restart: brand new store over the same DB.
        # The in-memory-only implementation FAILS here — this is the point.
        s2 = SessionStore(db)
        assert s2.verify(cid, tok), "session lost after restart"

    def test_in_memory_mode_still_works(self):
        from gateway.sessions import SessionStore
        s = SessionStore()  # no db path -> memory mode
        cid, tok = s.create()
        assert s.verify(cid, tok)

    def test_unknown_customer_still_rejected(self, tmp_path):
        from gateway.sessions import SessionStore
        s = SessionStore(str(tmp_path / "s.db"))
        assert not s.verify("nobody", "nope")


class TestServerUsesPersistentStore:
    def test_app_sessions_survive_new_app_instance(self, monkeypatch, tmp_path):
        """create_app() must wire a store backed by the wallet DB."""
        fd_db = tmp_path / "w.db"
        monkeypatch.setenv("TRYX402_DB_PATH", str(fd_db))

        from gateway.server import create_app
        from starlette.testclient import TestClient

        app1 = create_app()
        c1 = TestClient(app1)
        sess = c1.post("/v1/auth/session", json={}).json()

        # "Restart": fresh app instance, same DB
        app2 = create_app()
        c2 = TestClient(app2)
        r = c2.get("/v1/wallet/balance", headers={
            "X-Customer-ID": sess["customer_id"],
            "X-Session-Token": sess["token"],
        })
        assert r.status_code == 200, f"session lost across restart: {r.status_code}"


class TestSdkAutoRemint:
    """SDK: on 401 with a cached token, mint a new session and retry ONCE."""

    def _mk(self, code=401, payload=None):
        import urllib.error

        def make():
            return urllib.error.HTTPError(
                url="test", code=code, msg="Unauthorized", hdrs={},
                fp=__import__("io").BytesIO(
                    json.dumps(payload or {"detail": "Invalid"}).encode()))

        return make

    def test_proxy_call_remints_on_401(self, monkeypatch, tmp_path):
        from gateway import anon_auth
        monkeypatch.setattr(anon_auth, "CUSTOMER_ID_FILE", tmp_path / "cid.json")

        import urllib.request
        calls = []

        session_payloads = iter([
            {"customer_id": "cid-old", "token": "tok-stale"},
            {"customer_id": "cid-new", "token": "tok-fresh"},
        ])

        class Resp:
            def __init__(self, p):
                self._p = json.dumps(p).encode()
            def read(self):
                return self._p
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        err_cls = self._mk(401)

        def mock_urlopen(req, *a, **kw):
            calls.append(req)
            url = req.full_url
            if "/v1/auth/session" in url:
                return Resp(next(session_payloads))
            h = {k.lower(): v for k, v in req.headers.items()}
            if h.get("x-session-token") == "tok-stale":
                raise err_cls()
            return Resp({"status_code": 200, "headers": {}, "body": "ok",
                         "cost_cents": 10, "commission_cents": 50,
                         "total_cents": 60, "new_balance_cents": 940})

        monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

        from gateway.api import Gateway
        gw = Gateway(api_key=None)
        result = gw.proxy_call("https://known.example.com/a", body={})

        # final call used the fresh token and succeeded
        last = calls[-1]
        h = {k.lower(): v for k, v in last.headers.items()}
        assert h.get("x-session-token") == "tok-fresh"
        assert result["total_cents"] == 60

    def test_no_infinite_retry_loop(self, monkeypatch, tmp_path):
        from gateway import anon_auth
        monkeypatch.setattr(anon_auth, "CUSTOMER_ID_FILE", tmp_path / "cid.json")

        import urllib.request
        proxy_attempts = []

        class Resp:
            def __init__(self, p):
                self._p = json.dumps(p).encode()
            def read(self):
                return self._p
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        err_cls = self._mk(401)

        def mock_urlopen(req, *a, **kw):
            url = req.full_url
            if "/v1/auth/session" in url:
                return Resp({"customer_id": "cid-x", "token": "tok-x"})
            proxy_attempts.append(1)
            raise err_cls()

        monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

        from gateway.api import Gateway
        gw = Gateway(api_key=None)
        with pytest.raises(RuntimeError):
            gw.proxy_call("https://known.example.com/a", body={})
        assert len(proxy_attempts) <= 2, f"retried {len(proxy_attempts)} times — loop!"
