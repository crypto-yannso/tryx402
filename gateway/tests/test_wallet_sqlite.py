"""Tests for gateway.wallet_sqlite (persistent wallet).

Run: python3 -m pytest gateway/tests/test_wallet_sqlite.py -v
"""

import os
import tempfile
import pytest


class TestSQLiteWallet:
    """Tests for SQLite-backed persistent wallet."""

    def test_balance_survives_restart(self):
        from gateway.wallet_sqlite import SQLiteWallet
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            # First session: credit
            w1 = SQLiteWallet(db_path, customer_id="restart_001")
            w1.credit(amount_cents=1000, description="Deposit")
            assert w1.get_balance() == 1000
            del w1

            # Second session: verify balance persisted
            w2 = SQLiteWallet(db_path, customer_id="restart_001")
            assert w2.get_balance() == 1000
            w2.debit(amount_cents=300, description="Call")
            assert w2.get_balance() == 700
            del w2

            # Third session: verify final state
            w3 = SQLiteWallet(db_path, customer_id="restart_001")
            assert w3.get_balance() == 700
            history = w3.get_history()
            assert len(history) == 2
            assert history[0]["type"] == "credit"
            assert history[1]["type"] == "debit"
        finally:
            os.unlink(db_path)

    def test_multiple_customers_isolated(self):
        from gateway.wallet_sqlite import SQLiteWallet
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            w_a = SQLiteWallet(db_path, customer_id="iso_a")
            w_b = SQLiteWallet(db_path, customer_id="iso_b")
            w_a.credit(500, "Deposit A")
            w_b.credit(1000, "Deposit B")
            assert w_a.get_balance() == 500
            assert w_b.get_balance() == 1000
        finally:
            os.unlink(db_path)

    def test_insufficient_balance_raises(self):
        from gateway.wallet_sqlite import SQLiteWallet, InsufficientBalance
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            w = SQLiteWallet(db_path, customer_id="insuf_001")
            with pytest.raises(InsufficientBalance):
                w.debit(100, "Call")
        finally:
            os.unlink(db_path)

    def test_history_includes_stripe_session_id(self):
        from gateway.wallet_sqlite import SQLiteWallet
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            w = SQLiteWallet(db_path, customer_id="stripe_001")
            w.credit(1000, "Stripe recharge", stripe_session_id="cs_test_123")
            history = w.get_history()
            assert history[-1]["stripe_session_id"] == "cs_test_123"
        finally:
            os.unlink(db_path)
