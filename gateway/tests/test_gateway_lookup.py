import os
import sqlite3
import tempfile
import unittest
from gateway.wallet_email_index import EmailWalletIndex


class TestGatewayLookupByEmail(unittest.TestCase):
    """TDD: lookup customer_id and wallet from email."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db")
        self.db_path = self.tmp.name
        self.idx = EmailWalletIndex(self.db_path)

    def test_register_and_lookup(self):
        email = "yann@artaifact.com"
        cid = "cust-yann-001"
        self.idx.register_payment(email, cid)
        self.assertEqual(self.idx.lookup(email), cid)

    def test_lookup_unknown_returns_none(self):
        self.assertIsNone(self.idx.lookup("nobody@void.com"))

    def test_case_insensitive(self):
        self.idx.register_payment("Yann@Artaifact.Com", "cust-case-001")
        self.assertEqual(self.idx.lookup("yann@artaifact.com"), "cust-case-001")
        self.assertEqual(self.idx.lookup("YANN@ARTAIFACT.COM"), "cust-case-001")

    def test_multiple_payments_returns_latest(self):
        self.idx.register_payment("multi@test.com", "cust-old")
        self.idx.register_payment("multi@test.com", "cust-new")
        self.assertEqual(self.idx.lookup("multi@test.com"), "cust-new")

    def test_empty_string_returns_none(self):
        self.assertIsNone(self.idx.lookup(""))
        self.assertIsNone(self.idx.lookup(None))
        self.idx.register_payment("", "x")
        self.assertIsNone(self.idx.lookup(""))


if __name__ == "__main__":
    unittest.main()