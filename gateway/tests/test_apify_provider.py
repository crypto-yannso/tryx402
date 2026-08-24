"""Tests offline du wrapper Apify — mock urllib, zéro appel réseau."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gateway.apify_provider import (
    ApifyError,
    list_actors,
    normalize_actor,
    resolve_actor,
    run_actor_sync,
)


def _fake_response(payload):
    resp = mock.Mock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.status = 200
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    return resp


class TestApifyProvider(unittest.TestCase):
    @mock.patch("gateway.apify_provider.urllib.request.urlopen")
    def test_run_actor_sync_list_payload(self, urlopen):
        # 1er appel : run-sync (retourne les items) ; 2e : /runs (usage)
        urlopen.side_effect = [
            _fake_response([{"url": "https://a.com", "title": "A"}]),
            _fake_response({"data": {"items": [{"id": "run123"}]}}),
            _fake_response({"data": {"usage": {"totalUsd": 0.0021}}}),
        ]
        out = run_actor_sync("user/some-actor", {"startUrls": ["https://a.com"]},
                             token="tok", timeout_s=30, max_items=10)
        self.assertEqual(out["item_count"], 1)
        self.assertEqual(out["items"][0]["title"], "A")
        self.assertEqual(out["usage_total_usd"], 0.0021)   # coût réel lu
        self.assertEqual(out["apify_run_id"], "run123")
        self.assertEqual(out["clamped_inputs"], [])     # input sous plafonds
        # vérifie l'URL appelée : run-sync + token + limit
        run_url = urlopen.call_args_list[0][0][0].full_url
        self.assertIn("/acts/user~some-actor/run-sync-get-dataset-items", run_url)
        self.assertIn("token=tok", run_url)
        self.assertIn("limit=10", run_url)

    @mock.patch("gateway.apify_provider.urllib.request.urlopen")
    def test_run_clamps_expensive_input(self, urlopen):
        urlopen.side_effect = [
            _fake_response([]), _fake_response({"data": {"items": []}})]
        out = run_actor_sync(
            "u/x", {"startUrls": [{"url": f"https://x/{i}"} for i in range(40)],
                    "maxCrawlPages": 5000},
            token="t", fetch_cost=False)
        body = json.loads(urlopen.call_args_list[0][0][0].data)
        self.assertEqual(len(body["startUrls"]), 5)
        self.assertEqual(body["maxCrawlPages"], 5)
        self.assertEqual(sorted(out["clamped_inputs"]),
                         ["maxCrawlPages", "startUrls"])

    @mock.patch("gateway.apify_provider.urllib.request.urlopen")
    def test_memory_floor_and_ceiling(self, urlopen):
        urlopen.side_effect = [
            _fake_response([]), _fake_response({}), _fake_response({}),
            _fake_response({})]
        # 1) défaut : 1024 MB (pas de memory_mbytes)
        run_actor_sync("u/x", {}, token="t", fetch_cost=False)
        url1 = urlopen.call_args_list[0][0][0].full_url
        self.assertIn("memory=1024", url1)
        # 2) client demande 64 MB → remonté au plancher 128
        run_actor_sync("u/x", {}, token="t", fetch_cost=False, memory_mbytes=64)
        url2 = urlopen.call_args_list[1][0][0].full_url
        self.assertIn("memory=128", url2)
        # 3) client demande 16384 MB → plafonné à 4096
        run_actor_sync("u/x", {}, token="t", fetch_cost=False,
                       memory_mbytes=16384)
        url3 = urlopen.call_args_list[2][0][0].full_url
        self.assertIn("memory=4096", url3)

    @mock.patch("gateway.apify_provider.urllib.request.urlopen")
    def test_http_error_raises(self, urlopen):
        import urllib.error
        err = urllib.error.HTTPError("u", 400, "bad", {}, None)
        urlopen.side_effect = err
        with self.assertRaises(ApifyError) as ctx:
            run_actor_sync("user/x", token="t")
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_missing_token(self):
        from gateway.apify_provider import get_token
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ApifyError):
                get_token()

    @mock.patch("gateway.apify_provider.urllib.request.urlopen")
    def test_resolve_actor_by_name(self, urlopen):
        urlopen.return_value = _fake_response({"data": {
            "id": "abc", "username": "u", "name": "my-actor",
            "title": "My Actor", "description": "d", "isPrivate": False,
            "stats": {"totalRuns": 42}}})
        meta = resolve_actor("u/my-actor", token="t")
        self.assertEqual(meta["name"], "u/my-actor")
        self.assertEqual(meta["total_runs"], 42)

    def test_normalize_actor_safe(self):
        n = normalize_actor({})
        self.assertIn("name", n)

    def test_clamp_run_input(self):
        from gateway.apify_provider import clamp_run_input, MAX_START_URLS
        raw = {
            "startUrls": [{"url": f"https://x.com/{i}"} for i in range(50)],
            "maxCrawlPages": 9999,
            "maxResults": 5000,
            "maxRequestsPerCrawl": 10_000,
            "genuineField": "untouched",
        }
        clamped, touched = clamp_run_input(raw)
        self.assertEqual(len(clamped["startUrls"]), MAX_START_URLS)
        self.assertEqual(clamped["maxCrawlPages"], 5)
        self.assertEqual(clamped["maxResults"], 50)
        self.assertEqual(clamped["maxRequestsPerCrawl"], 5)
        self.assertEqual(clamped["genuineField"], "untouched")
        self.assertIn("maxCrawlPages", touched)

    def test_clamp_no_input(self):
        from gateway.apify_provider import clamp_run_input
        self.assertEqual(clamp_run_input(None), ({}, []))
        c, t = clamp_run_input({"maxCrawlPages": 2})
        self.assertEqual(c["maxCrawlPages"], 2)   # sous le plafond : intact
        self.assertEqual(t, [])

if __name__ == "__main__":
    unittest.main()
