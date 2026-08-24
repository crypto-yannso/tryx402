# x402-native facade: lets external AI agents (AWS AgentCore, Claude, Circle)
# pay tryx402 directly with the x402 protocol, so wrapped web2 tools can be
# listed on aggregators (Coinbase x402 Bazaar, x402scan).
import pytest

from gateway.x402_facade import build_accepts, FacadeConfigError


class TestBuildAccepts:
    def test_builds_exact_scheme_payload_from_price_cents(self):
        accepts = build_accepts(
            resource_url="https://tryx402.fly.dev/v1/x402/call",
            description="Apify web scrape",
            price_cents=5,
            pay_to="0xabc0000000000000000000000000000000000001",
        )
        assert isinstance(accepts, list) and len(accepts) >= 1
        entry = accepts[0]
        assert entry["scheme"] == "exact"
        assert entry["maxAmountRequired"] == "50000"  # $0.05 atomic USDC as string
        assert entry["maxTimeoutSeconds"] > 0
        assert entry["payTo"] == "0xabc0000000000000000000000000000000000001"
        assert entry["resource"] == "https://tryx402.fly.dev/v1/x402/call"
        assert entry["description"] == "Apify web scrape"

    def test_atomic_conversion_is_exact_not_float(self):
        # $0.05 * 1e6 must be exactly 50000 (no float artifacts like 49999.99)
        accepts = build_accepts(
            resource_url="https://x.test/r",
            description="d",
            price_cents=5,
            pay_to="0xabc0000000000000000000000000000000000001",
        )
        assert accepts[0]["maxAmountRequired"] == "50000"
        assert isinstance(accepts[0]["maxAmountRequired"], str)

    def test_network_and_asset_are_configurable(self):
        accepts = build_accepts(
            resource_url="https://x.test/r",
            description="d",
            price_cents=1,
            pay_to="0xabc0000000000000000000000000000000000001",
            network="solana-devnet",
            asset="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        )
        # wire format keeps the configured (legacy) name
        assert accepts[0]["network"] == "solana-devnet"
        assert accepts[0]["asset"] == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

    def test_defaults_are_base_mainnet_usdc(self):
        accepts = build_accepts(
            resource_url="https://x.test/r",
            description="d",
            price_cents=1,
            pay_to="0xabc0000000000000000000000000000000000001",
        )
        assert accepts[0]["network"] == "base"
        # Base mainnet USDC contract
        assert accepts[0]["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    def test_max_amount_required_is_string_for_sdk_compat(self):
        # The official x402 SDK's pydantic PaymentRequiredV1 model expects
        # maxAmountRequired as a STRING (v1 wire format). Ints break real
        # clients with "Input should be a valid string".
        accepts = build_accepts(
            resource_url="https://x.test/r",
            description="d",
            price_cents=5,
            pay_to="0xabc0000000000000000000000000000000000001",
        )
        assert accepts[0]["maxAmountRequired"] == "50000"
        assert isinstance(accepts[0]["maxAmountRequired"], str)

    def test_rejects_missing_pay_to(self):
        with pytest.raises(FacadeConfigError):
            build_accepts(
                resource_url="https://x.test/r",
                description="d",
                price_cents=1,
                pay_to="",
            )

    def test_rejects_zero_or_negative_price(self):
        with pytest.raises(FacadeConfigError):
            build_accepts(
                resource_url="https://x.test/r",
                description="d",
                price_cents=0,
                pay_to="0xabc0000000000000000000000000000000000001",
            )
