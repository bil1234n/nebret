"""
Receipt verification (bank transfers into a partner's account). Kept apart from the payment *gateways*: nothing is
collected here, a verifier only tells us what the bank says about a transfer.

``get_verifier()`` returns the configured implementation:

* :class:`ShegerPayVerifier` – ``POST {SHEGERPAY.BASE_URL}/api/v1/verify`` with ``X-API-Key`` (from ShegerPay's
  official SDK). Documented response fields: ``valid``/``verified``, ``status``, ``amount``, ``payer``, ``reason``,
  ``transaction_id``. Receiver account, transfer time and narration are read when ShegerPay returns them (under any of
  the usual names) – when it doesn't, the business rules decide what to do (see ``SHEGERPAY_RECEIVER_CHECK``).
* :class:`FakeVerifier` – deterministic, in memory, for tests and local development.

Verifiers never decide whether a receipt passes; they report facts. ``apps.market.services.bank_transfer`` applies
the rules (exact amount, receiver = the order's account, payer, time window, payment code, reference unused).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.utils.dateparse import parse_datetime

from .banks import SHEGERPAY_PROVIDER

log = logging.getLogger(__name__)


@dataclass
class VerificationRequest:
    channel: str                # apps.payments.banks.Channel
    reference: str              # normalised
    amount: Decimal
    receiver_account: str       # the order's snapshot account number
    receiver_name: str
    payer_account: str = ""     # needed by ShegerPay for some BOA receipts
    currency: str = "ETB"
    # the receipt itself (photo / PDF) – ShegerPay reads its QR code or text when there is no reference
    receipt: bytes | None = None
    receipt_name: str = "receipt.jpg"
    receipt_type: str = "image/jpeg"


@dataclass
class VerificationResult:
    """
    ``outcome``:
    * FOUND       – the bank knows the transfer and it is complete (facts below).
    * NOT_FOUND   – no such transfer (or the bank rejected it).
    * PENDING     – the bank shows it as still processing; check again later.
    * ERROR       – verifier unreachable / outage / bad response; retry with backoff.
    * UNSUPPORTED – this bank, wallet, currency or receipt type can't be verified automatically.
    """

    outcome: str
    amount: Decimal | None = None
    payer_name: str = ""
    receiver_account: str = ""
    receiver_name: str = ""
    transferred_at: datetime | None = None
    narration: str = ""
    transaction_id: str = ""
    reason: str = ""
    request: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


class ReceiptVerifier:
    name = "base"

    def verify(self, req: VerificationRequest) -> VerificationResult:  # pragma: no cover - interface
        raise NotImplementedError


def _first(d: dict, *keys):
    for k in keys:
        cur = d
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            return cur
    return None


def _decimal(v) -> Decimal | None:
    try:
        return Decimal(str(v)).quantize(Decimal("0.01")) if v not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


class ShegerPayVerifier(ReceiptVerifier):
    name = "shegerpay"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: int | None = None):
        cfg = getattr(settings, "SHEGERPAY", {})
        self.api_key = api_key or cfg.get("API_KEY", "")
        self.base_url = (base_url or cfg.get("BASE_URL") or "https://api.shegerpay.com").rstrip("/")
        self.timeout = timeout or cfg.get("TIMEOUT", 20)

    def verify(self, req: VerificationRequest) -> VerificationResult:
        """
        By reference (``POST /api/v1/verify``) when the buyer typed one; otherwise – or when the typed reference isn't
        found – from the receipt photo / PDF (``POST /api/v1/verify-image``, field ``screenshot``): ShegerPay reads
        the QR code or the text, detects the bank and checks it at the source.
        """
        provider = SHEGERPAY_PROVIDER.get(req.channel)
        if req.currency != "ETB" or (not provider and not req.receipt):
            return VerificationResult("UNSUPPORTED", reason=f"{req.channel} / {req.currency} can't be verified "
                                                            "automatically by ShegerPay.")
        if not self.api_key:
            return VerificationResult("ERROR", reason="SHEGERPAY_API_KEY is not set.")
        if not req.reference or not provider:
            return self._post(req, image=True, provider=provider)
        result = self._post(req, image=False, provider=provider)
        if result.outcome == "NOT_FOUND" and req.receipt:
            by_image = self._post(req, image=True, provider=provider)
            if by_image.outcome != "ERROR":
                return by_image
        return result

    def _post(self, req: VerificationRequest, *, image: bool, provider: str | None) -> VerificationResult:
        data = {"amount": str(req.amount), "merchant_name": req.receiver_name}
        if provider:
            data["provider"] = provider
        if req.reference:
            data["transaction_id"] = req.reference
        if req.payer_account:
            data["sender_account"] = req.payer_account
        safe_request = {**data, "sender_account": "•••" if req.payer_account else "",
                        "endpoint": "verify-image" if image else "verify"}
        try:
            if image:
                resp = requests.post(f"{self.base_url}/api/v1/verify-image", data=data, timeout=max(self.timeout, 45),
                                     files={"screenshot": (req.receipt_name, req.receipt, req.receipt_type)},
                                     headers={"X-API-Key": self.api_key, "User-Agent": "Nebret/1.0"})
            else:
                resp = requests.post(f"{self.base_url}/api/v1/verify", data=data, timeout=self.timeout,
                                     headers={"X-API-Key": self.api_key, "User-Agent": "Nebret/1.0"})
        except requests.RequestException as exc:
            return VerificationResult("ERROR", reason=f"ShegerPay unreachable: {exc}"[:255], request=safe_request)
        try:
            body = resp.json()
        except ValueError:
            body = {"text": resp.text[:500]}
        if resp.status_code in (401, 402, 403, 429) or resp.status_code >= 500:
            return VerificationResult("ERROR", reason=f"ShegerPay HTTP {resp.status_code}: "
                                                      f"{body.get('detail') or body.get('message') or ''}"[:255],
                                      request=safe_request, raw=body)
        if resp.status_code == 400:
            return VerificationResult("NOT_FOUND", reason=str(body.get("detail") or body.get("message")
                                                              or "Rejected by ShegerPay")[:255],
                                      request=safe_request, raw=body)
        return self.parse(body, safe_request)

    @staticmethod
    def parse(body: dict, request: dict | None = None) -> VerificationResult:
        status = str(body.get("status") or "").lower()
        ok = bool(body.get("verified", body.get("valid", False)))
        when = _first(body, "transaction_date", "date", "paid_at", "transferred_at", "data.date",
                      "data.transaction_date", "receipt.date")
        transferred_at = parse_datetime(str(when)) if when else None
        facts = dict(
            amount=_decimal(_first(body, "amount", "data.amount", "receipt.amount")),
            payer_name=str(_first(body, "payer", "payer_name", "sender_name", "data.payer", "receipt.payer") or ""),
            receiver_account=str(_first(body, "receiver_account", "credited_account", "data.receiver_account",
                                        "receipt.receiver_account") or ""),
            receiver_name=str(_first(body, "receiver", "receiver_name", "merchant", "data.receiver",
                                     "receipt.receiver_name") or ""),
            transferred_at=transferred_at,
            narration=str(_first(body, "narration", "reason_text", "description", "remark", "data.narration",
                                 "receipt.narration") or ""),
            transaction_id=str(_first(body, "transaction_id", "data.transaction_id") or ""),
            request=request or {}, raw=body)
        if status in ("pending", "processing", "in_progress"):
            return VerificationResult("PENDING", reason="The bank shows the transfer as pending.", **facts)
        if ok or status in ("verified", "success", "completed", "valid"):
            return VerificationResult("FOUND", **facts)
        return VerificationResult("NOT_FOUND", reason=str(body.get("reason") or body.get("message")
                                                          or "Transfer not found at the bank")[:255], **facts)


class FakeVerifier(ReceiptVerifier):
    """
    Register transfers the "bank" knows about::

        FakeVerifier.register("CBE", "FT123", amount="1000", payer="Meron Tadesse", receiver_account="1000123456",
                              transferred_at=dt, narration="PK7Q9M2")
        FakeVerifier.outage = True   # every call returns ERROR
    """

    name = "fake"
    transfers: dict[tuple[str, str], dict] = {}
    outage = False

    @classmethod
    def register(cls, channel: str, reference: str, **facts):
        cls.transfers[(channel.upper(), reference.upper())] = facts

    @classmethod
    def register_image(cls, receipt: bytes, channel: str, reference: str, **facts):
        """A receipt photo the fake "OCR" can read: it finds ``reference`` on it."""
        import hashlib

        cls.register(channel, reference, **facts)
        cls.images[hashlib.sha256(receipt).hexdigest()] = (channel.upper(), reference.upper())

    images: dict[str, tuple[str, str]] = {}

    @classmethod
    def reset(cls):
        cls.transfers, cls.images, cls.outage = {}, {}, False

    def verify(self, req: VerificationRequest) -> VerificationResult:
        if self.outage:
            return VerificationResult("ERROR", reason="Fake outage")
        if req.currency != "ETB" or (req.channel not in SHEGERPAY_PROVIDER and not req.receipt):
            return VerificationResult("UNSUPPORTED", reason="Not verifiable automatically")
        reference = req.reference
        t = self.transfers.get((req.channel, reference)) if reference else None
        if not t and req.receipt:
            import hashlib

            found = self.images.get(hashlib.sha256(req.receipt).hexdigest())
            if found:
                reference = found[1]
                t = self.transfers.get(found)
        req = VerificationRequest(**{**req.__dict__, "reference": reference})
        if not t:
            return VerificationResult("NOT_FOUND", reason="Transfer not found at the bank")
        if t.get("pending"):
            return VerificationResult("PENDING", reason="Pending at the bank")
        return VerificationResult(
            "FOUND", amount=_decimal(t.get("amount")), payer_name=t.get("payer", ""),
            receiver_account=t.get("receiver_account", ""), receiver_name=t.get("receiver_name", ""),
            transferred_at=t.get("transferred_at"), narration=t.get("narration", ""), transaction_id=req.reference,
            request={"channel": req.channel, "reference": req.reference}, raw={"fake": True})


def get_verifier() -> ReceiptVerifier:
    kind = settings.NEBRET.get("RECEIPT_VERIFIER", "fake")
    return ShegerPayVerifier() if kind == "shegerpay" else FakeVerifier()
