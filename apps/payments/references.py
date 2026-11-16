"""
Transfer references (bank FT numbers, mobile-money transaction ids) may be used to pay for something only once,
anywhere on Nebret – across orders, companies, office payments and uploaded receipts.

* :func:`normalise_reference` – uppercase, letters and digits only, so ``ft 24-123 abc`` and ``FT24123ABC`` match.
* :func:`check_unused` – raise :class:`DuplicateReference` (clear message) before anything is saved.
* :func:`claim` – record the reference inside the caller's transaction; the database constraint catches races.
* :func:`raise_alert` – tell Nebret staff. Call it *outside* the failed transaction, otherwise the rollback that
  follows the error also removes the alert.
"""
from __future__ import annotations

import logging
import re

from django.db import IntegrityError, transaction
from django.db.models import Q

from apps.core.exceptions import BusinessRuleError

from .models import BankReference, FraudAlert

log = logging.getLogger(__name__)
_NOT_ALNUM = re.compile(r"[^A-Z0-9]")


class DuplicateReference(BusinessRuleError):
    default_code = "duplicate_reference"

    def __init__(self, bank: str, reference: str, existing: BankReference | None = None):
        self.bank, self.reference, self.existing = bank, reference, existing
        super().__init__(f"Transfer reference {reference} has already been used for another payment. "
                         "Each bank transfer can only be used once. Nebret has been notified.")


def normalise_reference(value: str) -> str:
    return _NOT_ALNUM.sub("", (value or "").upper())[:64]


def normalise_bank(value: str) -> str:
    return _NOT_ALNUM.sub("", (value or "").upper())[:20] or BankReference.UNKNOWN_BANK


def _conflicts(bank: str, reference: str):
    """
    Same bank + reference is always a clash. When either side's bank is unknown (``BANK``), the reference alone is
    enough – except against mobile money, whose ids are a different numbering.
    """
    qs = BankReference.objects.filter(reference=reference)
    if bank == BankReference.MOBILE:
        return qs.filter(bank=BankReference.MOBILE)
    if bank == BankReference.UNKNOWN_BANK:
        return qs.exclude(bank=BankReference.MOBILE)
    return qs.filter(Q(bank=bank) | Q(bank=BankReference.UNKNOWN_BANK))


def check_unused(bank: str, reference: str) -> None:
    existing = _conflicts(bank, reference).select_related("payment").first()
    if existing:
        raise DuplicateReference(bank, reference, existing)


def claim(bank: str, reference: str, *, payment=None, source: str = "office") -> BankReference:
    """Record a reference as used. Call inside the transaction that records the payment."""
    check_unused(bank, reference)
    try:
        with transaction.atomic():
            return BankReference.objects.create(bank=bank, reference=reference, payment=payment, source=source)
    except IntegrityError as exc:  # someone claimed it between the check and the insert
        raise DuplicateReference(bank, reference, BankReference.objects.filter(bank=bank, reference=reference)
                                 .first()) from exc


def raise_alert(exc: DuplicateReference, *, user=None, agency=None, order_reference: str = "", context: str = "",
                amount=None) -> FraudAlert:
    """Record a duplicate-reference fraud alert and notify every active Nebret staff member."""
    from apps.accounts.models import User
    from apps.notifications.models import Notification
    from apps.notifications.services import notify

    earlier = exc.existing
    earlier_txn = getattr(earlier, "payment", None)
    earlier_order = ""
    if earlier_txn is not None:
        target = earlier_txn.target
        earlier_order = getattr(target, "reference", "") or getattr(getattr(target, "order", None), "reference", "")
    msg = (f"{exc.bank} reference {exc.reference} reused"
           + (f" on {order_reference}" if order_reference else "")
           + (f" (first used on {earlier_order})" if earlier_order else "") + (f" – {context}" if context else ""))
    log.error("FRAUD ALERT: %s", msg)
    alert = FraudAlert.objects.create(
        kind=FraudAlert.Kind.DUPLICATE_REFERENCE, message=msg[:255], user=user, agency=agency,
        order_reference=order_reference[:20], existing=earlier,
        data={"bank": exc.bank, "reference": exc.reference, "amount": str(amount) if amount is not None else "",
              "earlier_order": earlier_order, "earlier_payment": str(getattr(earlier_txn, "reference", "") or ""),
              "context": context})
    for staff in User.objects.filter(is_staff=True, is_active=True):
        notify(staff, Notification.Kind.PAYMENT, "Fraud alert: transfer reference reused", msg[:500],
               {"fraud_alert": alert.pk, **({"order": order_reference} if order_reference else {})})
    return alert
