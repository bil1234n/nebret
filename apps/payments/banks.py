"""
Banks and wallets used for marketplace bank transfers.

Partner companies can only receive into banks ShegerPay verifies (CBE, Bank of Abyssinia, Awash, Dashen – confirmed
from ShegerPay's official SDK, Nov 2026). A buyer may send from any of those banks or from a wallet ShegerPay verifies
(telebirr, CBE Birr). Anything else – another bank, an RTGS/corporate slip, a USD/EUR (SWIFT) transfer – can't be
checked automatically and goes to manual Nebret review before the seller confirms it.
"""
from django.db import models


class Bank(models.TextChoices):
    CBE = "CBE", "Commercial Bank of Ethiopia"
    BOA = "BOA", "Bank of Abyssinia"
    AWASH = "AWASH", "Awash Bank"
    DASHEN = "DASHEN", "Dashen Bank"


class Channel(models.TextChoices):
    """Where the buyer's receipt comes from (the sending bank or wallet)."""

    CBE = "CBE", "Commercial Bank of Ethiopia"
    BOA = "BOA", "Bank of Abyssinia"
    AWASH = "AWASH", "Awash Bank"
    DASHEN = "DASHEN", "Dashen Bank"
    TELEBIRR = "TELEBIRR", "telebirr"
    CBEBIRR = "CBEBIRR", "CBE Birr"
    OTHER = "OTHER", "Another bank (checked by Nebret)"


class Currency(models.TextChoices):
    ETB = "ETB", "Ethiopian Birr"
    USD = "USD", "US Dollar"
    EUR = "EUR", "Euro"


# ShegerPay provider codes. Channels missing here are verified manually.
SHEGERPAY_PROVIDER = {"CBE": "cbe", "BOA": "boa", "AWASH": "awash", "DASHEN": "dashen", "TELEBIRR": "telebirr",
                      "CBEBIRR": "cbebirr"}


def auto_verifiable(channel: str, currency: str = "ETB") -> bool:
    return channel in SHEGERPAY_PROVIDER and currency == "ETB"


def commission_account() -> dict:
    """Nebret's bank account that partner companies transfer the commission into ({} when not configured)."""
    from django.conf import settings

    acc = settings.NEBRET.get("COMMISSION_BANK_ACCOUNT") or settings.NEBRET.get("HOUSE_BANK_ACCOUNT") or {}
    if not acc.get("account_number"):
        return {}
    return {**acc, "bank_name": dict(Bank.choices).get(acc.get("bank"), acc.get("bank"))}
