# Wire format rules: the 402 payload carries the LEGACY network name (v1
# clients register by legacy name); the facilitator verify/settle call
# carries the CAIP-2 canonical name (facilitators register by CAIP-2).
import pytest

from gateway.x402_facade import build_accepts, DEFAULT_NETWORK


class TestNetworkNames:
    def test_default_wire_network_is_legacy_base(self):
        accepts = build_accepts(
            resource_url="https://x.test/r", description="d",
            price_cents=5, pay_to="0xabc0000000000000000000000000000000000001",
        )
        assert accepts[0]["network"] == "base"

    def test_configured_sepolia_passes_through(self):
        accepts = build_accepts(
            resource_url="https://x.test/r", description="d",
            price_cents=1, pay_to="0xabc0000000000000000000000000000000000001",
            network="base-sepolia",
        )
        assert accepts[0]["network"] == "base-sepolia"

    def test_canonical_network_maps_for_facilitator(self):
        from gateway.x402_payments import canonical_network
        assert canonical_network("base") == "eip155:8453"
        assert canonical_network("base-sepolia") == "eip155:84532"
        assert canonical_network("eip155:8453") == "eip155:8453"
