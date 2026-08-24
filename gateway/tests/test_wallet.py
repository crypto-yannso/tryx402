"""Tests for gateway.wallet (in-memory, then DB-backed).

Run: python3 -m pytest gateway/tests/test_wallet.py -v
"""

import time
import pytest

# We'll import from gateway.billing and gateway.wallet
# These imports will fail initially (TDD red phase)


class TestWallet:
    """Tests for the wallet: balance, credit, debit, history."""

    def test_initial_balance_is_zero(self):
        from gateway.wallet import Wallet
        wallet = Wallet(customer_id="test_001")
        assert wallet.get_balance() == 0

    def test_credit_increases_balance(self):
        from gateway.wallet import Wallet
        wallet = Wallet(customer_id="test_002")
        wallet.credit(amount_cents=1000, description="Initial deposit")
        assert wallet.get_balance() == 1000

    def test_debit_decreases_balance(self):
        from gateway.wallet import Wallet
        wallet = Wallet(customer_id="test_003")
        wallet.credit(amount_cents=1000, description="Initial deposit")
        wallet.debit(amount_cents=300, description="x402 call")
        assert wallet.get_balance() == 700

    def test_debit_insufficient_balance_raises(self):
        from gateway.wallet import Wallet, InsufficientBalance
        wallet = Wallet(customer_id="test_004")
        with pytest.raises(InsufficientBalance):
            wallet.debit(amount_cents=100, description="x402 call")

    def test_transaction_history(self):
        from gateway.wallet import Wallet
        wallet = Wallet(customer_id="test_005")
        wallet.credit(amount_cents=1000, description="Deposit")
        wallet.debit(amount_cents=200, description="Call 1")
        wallet.debit(amount_cents=300, description="Call 2")
        history = wallet.get_history()
        assert len(history) == 3
        assert history[0]["type"] == "credit"
        assert history[0]["amount_cents"] == 1000
        assert history[1]["type"] == "debit"
        assert history[1]["amount_cents"] == 200

    def test_balance_per_customer_is_isolated(self):
        from gateway.wallet import Wallet
        w1 = Wallet(customer_id="test_006a")
        w2 = Wallet(customer_id="test_006b")
        w1.credit(amount_cents=500, description="Deposit")
        assert w1.get_balance() == 500
        assert w2.get_balance() == 0


class TestStripeIntegration:
    """Tests for Stripe checkout session creation."""

    def test_create_checkout_session_requires_api_key(self):
        from gateway.billing import StripeBilling, StripeConfigError
        billing = StripeBilling(secret_key="sk_test_dummy")
        with pytest.raises(StripeConfigError):
            billing.create_checkout_session("test@example.com")

    def test_create_checkout_session_returns_url(self):
        """This test will be skipped unless STRIPE_TEST_KEY is set."""
        import os
        if not os.environ.get("STRIPE_TEST_KEY"):
            pytest.skip("STRIPE_TEST_KEY not set")
        from gateway.billing import StripeBilling
        billing = StripeBilling()
        session = billing.create_checkout_session(
            "test@example.com",
            mode="payment",
            metadata={"customer_id": "test_007"},
        )
        assert "url" in session
        assert session["url"].startswith("https://checkout.stripe.com")

    def test_webhook_verification_rejects_invalid_signature(self):
        from gateway.billing import verify_webhook, StripePaymentError
        with pytest.raises(StripePaymentError):
            verify_webhook(
                payload_bytes=b'{"type":"checkout.session.completed"}',
                sig_header="v1=invalid",
                webhook_secret="whsec_test",
            )

    def test_webhook_verification_accepts_valid_signature(self):
        from gateway.billing import verify_webhook
        import hmac, hashlib, json
        secret = "whsec_test"
        payload = json.dumps({"type": "checkout.session.completed"}).encode()
        # Simulate Stripe signature: timestamp.signature
        timestamp = str(int(time.time()))
        signed_payload = f"{timestamp}.{payload.decode()}"
        signature = hmac.new(
            secret.encode(), signed_payload.encode(), hashlib.sha256
        ).hexdigest()
        sig_header = f"t={timestamp},v1={signature}"
        event = verify_webhook(payload, sig_header, secret)
        assert event["type"] == "checkout.session.completed"


class TestWalletStripeFlow:
    """End-to-end test: recharge via Stripe → wallet credited."""

    def test_recharge_flow_simulated(self):
        """Simulate the flow without real Stripe API."""
        from gateway.wallet import Wallet
        wallet = Wallet(customer_id="test_008")
        # Simulate a successful Stripe webhook
        wallet.credit(amount_cents=1000, description="Stripe recharge", stripe_session_id="cs_test_123")
        assert wallet.get_balance() == 1000
        history = wallet.get_history()
        assert history[-1]["stripe_session_id"] == "cs_test_123"
