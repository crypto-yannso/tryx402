"""Auto-recharge manager for tryx402 wallets.

Handles:
- Stripe Billing subscription lifecycle (activate/cancel)
- Monthly credit on subscription activation
- Auto-recharge when balance falls below threshold
- Webhook event routing (checkout.session.completed, subscription updates)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .billing import StripeBilling, StripeConfigError
from .wallet import Wallet, InsufficientBalance

__all__ = ["AutoRechargeManager"]


class AutoRechargeManager:
    """Manage automatic wallet recharging via Stripe Billing.

    Args:
        wallet: the customer's wallet instance.
        monthly_credit_cents: credit amount when subscription activates.
        auto_recharge_threshold_cents: balance below this triggers a checkout.
    """

    def __init__(
        self,
        wallet: Wallet,
        *,
        monthly_credit_cents: int = 5000,
        auto_recharge_threshold_cents: int = 2000,
    ) -> None:
        self.wallet = wallet
        self.monthly_credit_cents = monthly_credit_cents
        self.auto_recharge_threshold_cents = auto_recharge_threshold_cents
        self._activated_subscriptions: set = set()
        self._active: bool = False

    # ------------------------------------------------------------------
    # Subscription lifecycle
    # ------------------------------------------------------------------

    def on_subscription_active(self, subscription_id: str) -> None:
        """Called when a Stripe subscription becomes active/trialing."""
        if subscription_id in self._activated_subscriptions:
            return
        self._activated_subscriptions.add(subscription_id)
        self._active = True
        self.wallet.credit(
            amount_cents=self.monthly_credit_cents,
            description=f"Subscription credit ({subscription_id})",
        )

    def on_subscription_cancelled(self, subscription_id: str) -> None:
        """Called when a Stripe subscription is cancelled."""
        self._activated_subscriptions.discard(subscription_id)
        if not self._activated_subscriptions:
            self._active = False

    def is_subscription_active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    # Auto-recharge logic
    # ------------------------------------------------------------------

    def should_recharge(self) -> bool:
        """Return True if balance is below the auto-recharge threshold."""
        return self.wallet.get_balance() < self.auto_recharge_threshold_cents

    def trigger_recharge(self, customer_email: str, amount_cents: Optional[int] = None) -> Dict[str, str]:
        """Create a Stripe Checkout session for a one-time top-up.

        Args:
            customer_email: the customer's email.
            amount_cents: top-up amount in cents (default: monthly_credit_cents).

        Returns:
            Stripe session dict with `url` and `session_id`.
        """
        try:
            billing = StripeBilling()
        except Exception:
            raise RuntimeError("Stripe is not configured. Set TRYX402_STRIPE_SECRET_KEY.")

        recharge_amount = amount_cents or self.monthly_credit_cents
        import os
        os.environ["TRYX402_STRIPE_AMOUNT_CENTS"] = str(recharge_amount)
        os.environ["TRYX402_STRIPE_CURRENCY"] = "eur"

        session = billing.create_checkout_session(
            customer_email,
            mode="payment",
            metadata={"customer_id": self.wallet.customer_id},
        )
        return {"url": session["url"], "session_id": session.get("id", "")}

    # ------------------------------------------------------------------
    # Webhook routing
    # ------------------------------------------------------------------

    def handle_webhook_event(self, event: Dict[str, Any]) -> None:
        """Route a Stripe webhook event to the appropriate handler."""
        event_type = event.get("type", "")
        data = event.get("data", {}).get("object", {})

        if event_type == "checkout.session.completed":
            self._handle_checkout_completed(data)
        elif event_type == "customer.subscription.updated":
            self._handle_subscription_updated(data)
        elif event_type == "customer.subscription.deleted":
            self._handle_subscription_deleted(data)

    def _handle_checkout_completed(self, session: Dict[str, Any]) -> None:
        mode = session.get("mode", "")
        if mode == "subscription":
            subscription_id = session.get("subscription", "")
            customer_id = session.get("client_reference_id") or session.get("customer", "")
            if subscription_id:
                self.on_subscription_active(subscription_id)

    def _handle_subscription_updated(self, subscription: Dict[str, Any]) -> None:
        status = subscription.get("status", "")
        subscription_id = subscription.get("id", "")
        if status in ("active", "trialing"):
            self.on_subscription_active(subscription_id)
        elif status in ("canceled", "incomplete_expired"):
            self.on_subscription_cancelled(subscription_id)

    def _handle_subscription_deleted(self, subscription: Dict[str, Any]) -> None:
        subscription_id = subscription.get("id", "")
        self.on_subscription_cancelled(subscription_id)
