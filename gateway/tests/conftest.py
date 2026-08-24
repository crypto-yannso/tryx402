# Shared fixtures: reset module-level mutable state between tests so the
# payment replay cache never leaks across test cases.
import pytest


@pytest.fixture(autouse=True)
def _reset_payment_state():
    import gateway.x402_payments as xp

    xp._seen_payments.clear()
    xp._receipt_builder = None
    yield
    xp._seen_payments.clear()
    xp._receipt_builder = None
