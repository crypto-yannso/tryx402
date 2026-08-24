# Syndication export: turn the hosted registry into aggregator listing
# documents so tryx402 tools can be listed on Coinbase x402 Bazaar,
# x402scan, and any x402 discovery index — one source of truth, N outputs.
import json
import pytest


class TestSyndicationExport:
    def _registry_with_tools(self):
        from gateway.registry import PriceRegistry
        reg = PriceRegistry()
        reg.register("https://api.apify.com", price_cents=5)
        reg.register("https://serpapi.com", price_cents=10)
        reg.register("http://localhost:9", price_cents=1, allow_private=True)
        return reg

    def _export(self, registry, base_url="https://tryx402.fly.dev",
                pay_to="0xabc0000000000000000000000000000000000001"):
        from gateway.syndication import export_listing
        return export_listing(registry, base_url=base_url, pay_to=pay_to)

    def test_export_returns_x402_resources_shape(self):
        doc = self._export(self._registry_with_tools())
        assert doc["x402Version"] == 1
        resources = {r["origin"]: r for r in doc["resources"]}
        # private origin never exported
        assert "http://localhost:9" not in resources
        apify = resources["https://api.apify.com"]
        # v1 wire format: amounts are strings (official SDK pydantic requirement)
        assert apify["maxAmountRequired"] == "50000"
        assert isinstance(apify["maxAmountRequired"], str)
        assert apify["payTo"].startswith("0x")

    def test_export_is_json_serializable_and_stable_ordered(self):
        import json as _json
        doc = self._export(self._registry_with_tools())
        s1 = _json.dumps(doc, sort_keys=True)
        s2 = _json.dumps(self._export(self._registry_with_tools()), sort_keys=True)
        assert s1 == s2  # deterministic output (crawlable diffs)

    def test_export_without_pay_to_raises_config_error(self):
        from gateway.syndication import SyndicationConfigError
        with pytest.raises(SyndicationConfigError):
            self._export(self._registry_with_tools(), pay_to="")

    def test_export_writes_document_to_disk(self, tmp_path):
        from gateway.syndication import write_listing
        out = tmp_path / "listing.json"
        n = write_listing(
            self._registry_with_tools(), out,
            pay_to="0xabc0000000000000000000000000000000000001")
        assert n == 2  # two public origins
        data = json.loads(out.read_text())
        assert len(data["resources"]) == 2

    def test_bazaar_feed_format_has_required_fields(self):
        from gateway.syndication import bazaar_feed
        feed = bazaar_feed(self._registry_with_tools(),
                           base_url="https://tryx402.fly.dev",
                           pay_to="0xabc0000000000000000000000000000000000001")
        item = feed[0]
        # fields aggregators minimally require to index a paid endpoint
        for field in ("id", "endpoint", "method", "price", "scheme",
                      "network", "payTo"):
            assert field in item, f"missing {field}"
        assert int(item["price"]) > 0  # string amounts (v1 wire format)
