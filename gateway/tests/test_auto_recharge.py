"""Tests for gateway.auto_recharge (Stripe Billing subscription).

Run: python3 -m pytest gateway/tests/test_auto_recharge.py -v
"""

import time
import pytest


class TestAutoRecharge:
    """Tests for automatic wallet recharge via Stripe Billing."""

    def test_subscription_credits_wallet_on_activate(self):
        from gateway.auto_recharge import AutoRechargeManager
        from gateway.wallet import Wallet
        
        wallet = Wallet(customer_id="autorecharge_001")
        manager = AutoRechargeManager(wallet, monthly_credit_cents=5000)
        
        # Simulate subscription activation
        manager.on_subscription_active(subscription_id="sub_001")
        
        # Wallet should be credited
        assert wallet.get_balance() == 5000
        history = wallet.get_history()
        assert history[-1]["type"] == "credit"
        assert "subscription" in history[-1]["description"].lower()

    def test_no_duplicate_credit_on_repeated_activation(self):
        from gateway.auto_recharge import AutoRechargeManager
        from gateway.wallet import Wallet
        
        wallet = Wallet(customer_id="autorecharge_002")
        manager = AutoRechargeManager(wallet, monthly_credit_cents=5000)
        
        manager.on_subscription_active(subscription_id="sub_002")
        manager.on_subscription_active(subscription_id="sub_002")  # duplicate
        
        # Should credit only once
        assert wallet.get_balance() == 5000
        history = wallet.get_history()
        assert len([h for h in history if h["type"] == "credit"]) == 1

    def test_recharge_when_balance_below_threshold(self):
        from gateway.auto_recharge import AutoRechargeManager
        from gateway.wallet import Wallet
        
        wallet = Wallet(customer_id="autorecharge_003")
        wallet.credit(amount_cents=3000, description="Initial")
        manager = AutoRechargeManager(
            wallet,
            monthly_credit_cents=5000,
            auto_recharge_threshold_cents=2000,
        )
        
        # Balance is 3000, above threshold → no recharge
        assert not manager.should_recharge()
        
        # Simulate spending below threshold
        wallet.debit(amount_cents=1500, description="Call")
        assert wallet.get_balance() == 1500
        assert manager.should_recharge()

    def test_recharge_triggers_checkout(self, monkeypatch):
        from gateway.auto_recharge import AutoRechargeManager
        from gateway.wallet import Wallet
        
        wallet = Wallet(customer_id="autorecharge_004")
        wallet.credit(amount_cents=1500, description="Initial")
        manager = AutoRechargeManager(
            wallet,
            monthly_credit_cents=5000,
            auto_recharge_threshold_cents=2000,
        )
        
        # Mock StripeBilling to avoid real API call
        class MockBilling:
            def create_checkout_session(self, email, *, mode="payment", metadata=None):
                return {"url": "https://checkout.stripe.com/cs_test", "id": "cs_test_123"}
        
        monkeypatch.setattr("gateway.auto_recharge.StripeBilling", MockBilling)
        
        result = manager.trigger_recharge(customer_email="test@example.com")
        assert "url" in result
        assert "session_id" in result

    def test_subscription_cancellation_marks_inactive(self):
        from gateway.auto_recharge import AutoRechargeManager
        from gateway.wallet import Wallet
        
        wallet = Wallet(customer_id="autorecharge_005")
        manager = AutoRechargeManager(wallet, monthly_credit_cents=5000)
        
        manager.on_subscription_active(subscription_id="sub_005")
        manager.on_subscription_cancelled(subscription_id="sub_005")
        
        assert not manager.is_subscription_active()

    def test_webhook_updates_subscription_status(self):
        from gateway.auto_recharge import AutoRechargeManager
        from gateway.wallet import Wallet
        
        wallet = Wallet(customer_id="autorecharge_006")
        manager = AutoRechargeManager(wallet, monthly_credit_cents=5000)
        
        # Simulate webhook: checkout.session.completed
        manager.handle_webhook_event({
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_001",
                    "customer": "cus_001",
                    "subscription": "sub_006",
                    "mode": "subscription",
                }
            }
        })
        
        assert manager.is_subscription_active()
        assert wallet.get_balance() == 5000
