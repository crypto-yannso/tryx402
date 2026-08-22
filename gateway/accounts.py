"""Multi-currency billing + margin — the 'Stripe-for-x402' layer (option 2).

Provider cost is in USD (the rail settles in USDC). The CUSTOMER account can be
denominated in ANY currency — EUR, USD, GBP, JPY… — so the gateway is not locked
to one region. Balances are integer **minor units of the account's own currency**
(no float money), and non-2-decimal currencies (JPY, KWD…) are handled. Funding
here is a STUB (`fund`) standing in for the Stripe webhook.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
from dataclasses import asdict, dataclass, field

# Minor-unit exponent per currency (default 2). Common exceptions:
_EXPONENT = {"JPY": 0, "KRW": 0, "CLP": 0, "VND": 0, "ISK": 0,
             "BHD": 3, "KWD": 3, "OMR": 3, "TND": 3}


def minor_factor(currency: str) -> int:
    return 10 ** _EXPONENT.get(currency.upper(), 2)


def to_minor(amount_major: float, currency: str) -> int:
    return int(round(amount_major * minor_factor(currency)))


def format_amount(minor: int, currency: str) -> str:
    exp = _EXPONENT.get(currency.upper(), 2)
    return f"{minor / (10 ** exp):.{exp}f} {currency.upper()}"


class InsufficientBalance(RuntimeError):
    pass


class UnknownCurrency(RuntimeError):
    pass


@dataclass
class FxRates:
    """USD -> currency multipliers. STUB defaults; wire a live FX feed in prod."""
    rates: dict = field(default_factory=lambda: {
        "USD": 1.0, "EUR": 0.92, "GBP": 0.79, "CHF": 0.88,
        "CAD": 1.37, "AUD": 1.52, "JPY": 150.0, "INR": 83.0, "BRL": 5.0,
    })

    def usd_to(self, currency: str) -> float:
        c = currency.upper()
        if c not in self.rates:
            raise UnknownCurrency(f"no FX rate configured for {c}")
        return self.rates[c]


@dataclass
class Account:
    id: str
    currency: str = "USD"
    balance_minor: int = 0
    margin: float = 0.30
    api_key_hash: str | None = None   # sha256 hex of the API key — never the key itself


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key() -> str:
    """Generate a fresh API key: 'gw_' + 32 urlsafe chars. Shown ONCE."""
    return "gw_" + secrets.token_urlsafe(24)


def price_minor(data_cost_usd: float, currency: str, rates: FxRates, margin: float) -> int:
    """End-user charge in integer minor units of `currency`. Rounds UP — never undercharge."""
    amount = data_cost_usd * rates.usd_to(currency) * (1.0 + margin)
    return int(math.ceil(round(amount, 10) * minor_factor(currency)))


@dataclass
class AccountStore:
    path: str
    accounts: dict = field(default_factory=dict)
    seen_path: str | None = None   # where processed webhook event ids persist

    @classmethod
    def load(cls, path):
        store = cls(path=path)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            store.accounts = {k: Account(**v) for k, v in raw.items()}
        return store

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({k: asdict(v) for k, v in self.accounts.items()}, f, indent=2)

    def create(self, account_id, currency="USD", margin=0.30) -> Account:
        acct = self.accounts.get(account_id) or Account(id=account_id)
        acct.currency = currency.upper()
        acct.margin = margin
        self.accounts[account_id] = acct
        return acct

    def fund(self, account_id, amount_major) -> Account:
        """STUB for the Stripe webhook: credit the account in its own currency."""
        acct = self.accounts.setdefault(account_id, Account(id=account_id))
        acct.balance_minor += to_minor(amount_major, acct.currency)
        return acct

    def credit_minor(self, account_id, minor, currency=None, rates: "FxRates | None" = None) -> Account:
        """Credit raw minor units — used by the Stripe webhook (Stripe amounts are
        already in the charge currency's minor units).

        Currency rules:
          * no stored account yet  -> the account is CREATED denominated in the
            payment's currency (a first EUR payment makes an EUR account);
          * matching currencies    -> straight credit;
          * mismatch               -> converted USD-side via `rates` (payment ->
            USD -> account currency), rounding UP so we never undercredit.
            Pass rates=None to REJECT mismatches instead (strict mode).
        """
        cur = (currency or "").upper() or None
        acct = self.accounts.get(account_id)
        if acct is None:
            acct = Account(id=account_id, currency=(cur or "USD"))
            self.accounts[account_id] = acct
        minor = int(minor)
        if cur and cur != acct.currency.upper():
            if rates is None:
                raise ValueError(
                    f"payment in {cur} but account {account_id} is {acct.currency} "
                    f"(pass FxRates to enable conversion)")
            usd_major = minor / minor_factor(cur) / rates.usd_to(cur)
            target = usd_major * rates.usd_to(acct.currency)
            import math as _m
            minor = int(_m.ceil(round(target, 10) * minor_factor(acct.currency)))
        acct.balance_minor += minor
        return acct

    def authorize(self, account_id, data_cost_usd, rates: FxRates) -> int:
        acct = self.accounts[account_id]
        charge = price_minor(data_cost_usd, acct.currency, rates, acct.margin)
        if charge > acct.balance_minor:
            raise InsufficientBalance(
                f"account {account_id}: need {format_amount(charge, acct.currency)}, "
                f"have {format_amount(acct.balance_minor, acct.currency)}")
        return charge

    def charge(self, account_id, minor) -> Account:
        acct = self.accounts[account_id]
        acct.balance_minor -= int(minor)
        return acct

    # --- multi-tenant auth (API keys) -----------------------------------------

    def issue_api_key(self, account_id) -> str:
        """Generate + store (hashed) an API key for the account. The plaintext
        key is returned ONCE — only its sha256 lives in the store."""
        acct = self.accounts[account_id]
        key = new_api_key()
        acct.api_key_hash = _hash_key(key)
        return key

    def authenticate(self, api_key: str) -> Account:
        """Resolve an API key to its Account. Raises PermissionError if unknown."""
        if not api_key:
            raise PermissionError("missing API key")
        h = _hash_key(api_key)
        for acct in self.accounts.values():
            if acct.api_key_hash and hmac.compare_digest(acct.api_key_hash, h):
                return acct
        raise PermissionError("unknown API key")
