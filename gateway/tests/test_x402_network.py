# The facilitator (x402.org / CDP) registers networks by CAIP-2 id
# ("eip155:8453"), not by legacy alias ("base"). Our 402 payloads and verify
# calls must use the canonical name the facilitator expects.
import pytest

from gateway.x402_facade import build_accepts


class TestCanonicalNetworkNames:
    def test_legacy_base_alias_maps_to_caip2(self):
        from gateway.x402_payments import canonical_network
        assert canonical_network("base") == "eip155:8453"

    def test_caip2_names_pass_through(self):
        from gateway.x402_payments import canonical_network
        assert canonical_network("eip155:8453") == "eip155:8453"
        assert canonical_network("solana:mainnet") == "solana:mainnet"

    def test_accepts_payload_uses_canonical_name(self):
        accepts = build_accepts(
            resource_url="https://x.test/r", description="d",
            price_cents=5, pay_to="0xabc0000000000000000000000000000000000001",
        )
        # default network must already be the CAIP-2 form
        assert accepts[0]["network"] == "eip155:8453"
