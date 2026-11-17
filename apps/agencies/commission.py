"""
Commission the company owes Nebret (money buyers paid straight into the company's account).

The company transfers it to Nebret's commission account and uploads the receipt; ShegerPay checks it against that
account (same verifier as buyers' receipts, reference optional – it can read it from the photo). Verified payments
mark the oldest commission due as collected; anything extra is kept as credit for the next commission.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify

from .models import Agency, AgencyLedgerEntry, AgencyStaff, CommissionPayment

log = logging.getLogger(__name__)
CP = CommissionPayment.Status
Payout = AgencyLedgerEntry.Payout
ZERO = Decimal("0.00")


def credit(agency: Agency) -> Decimal:
    """Verified commission payments not yet used for any commission."""
    return agency.commission_payments.filter(status=CP.VERIFIED).aggregate(s=Sum("unallocated"))["s"] or ZERO


def due(agency: Agency) -> Decimal:
    owed = agency.ledger.filter(payout_status__in=AgencyLedgerEntry.NEBRET_RECEIVABLE).aggregate(
        s=Sum("commission"))["s"] or ZERO
    return max(owed - credit(agency), ZERO)


def in_review(agency: Agency) -> Decimal:
    return agency.commission_payments.filter(status__in=[CP.SUBMITTED, CP.MANUAL]).aggregate(
        s=Sum("amount"))["s"] or ZERO


def _staff_notify(title, body, data):
    from apps.accounts.models import User

    for u in User.objects.filter(is_staff=True, is_active=True):
        notify(u, Notification.Kind.PAYMENT, title, body, data)


def submit(agency: Agency, user, *, file, bank: str, amount, transfer_datetime, reference: str = "",
           payer_name: str = "") -> CommissionPayment:
    from apps.core import private_files
    from apps.payments import references
    from apps.payments.banks import Channel, commission_account

    member = agency.staff.filter(user=user, is_active=True).first()
    if not (getattr(user, "is_staff", False) or (member and member.role in (AgencyStaff.Role.OWNER,
                                                                             AgencyStaff.Role.MANAGER))):
        raise BusinessRuleError("Only the company owner or a manager can send commission payments.")
    acc = commission_account()
    if not acc:
        raise BusinessRuleError("Nebret's commission account isn't set up yet – contact Nebret support.")
    data, content_type = private_files.validate(file)
    bank = (bank or "").upper()
    if bank not in Channel.values:
        raise BusinessRuleError("Choose the bank or wallet the money was sent from.")
    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except Exception as exc:  # noqa: BLE001
        raise BusinessRuleError("Enter the amount you transferred.") from exc
    if amount <= 0:
        raise BusinessRuleError("Enter the amount you transferred.")
    ref = references.normalise_reference(reference or "")
    if ref:
        if len(ref) < 6:
            raise BusinessRuleError("That transaction reference looks too short.")
        references.check_unused(bank, ref)
    if isinstance(transfer_datetime, str):
        from django.utils.dateparse import parse_datetime

        transfer_datetime = parse_datetime(transfer_datetime)
    if not transfer_datetime:
        raise BusinessRuleError("Enter the date and time of the transfer.")
    if timezone.is_naive(transfer_datetime):
        transfer_datetime = timezone.make_aware(transfer_datetime)
    if transfer_datetime > timezone.now() + timedelta(minutes=30):
        raise BusinessRuleError("The transfer time is in the future.")
    key = private_files.store(data, content_type, folder=f"commission/{agency.pk}", name=getattr(file, "name", ""))
    with transaction.atomic():
        cp = CommissionPayment.objects.create(
            agency=agency, amount=amount, bank=bank, reference=ref, transfer_datetime=transfer_datetime,
            payer_name=" ".join((payer_name or "").split())[:120], file=key, submitted_by=user, paid_to=acc,
            next_check_at=timezone.now())
        if ref:
            references.claim(bank, ref, source="commission")
    _staff_notify("Commission payment received", f"{agency.name} sent {amount:,.2f} ETB commission – checking it "
                                                 "with the bank.", {"commission_payment": cp.pk})
    if settings.NEBRET.get("VERIFY_ON_SUBMIT", True):
        try:
            cp = verify(cp)
        except Exception:  # noqa: BLE001
            log.exception("Commission verification failed for %s", cp.pk)
    return cp


def verify(cp: CommissionPayment) -> CommissionPayment:
    """Check one commission receipt with the bank (network call outside any transaction)."""
    from apps.core import private_files
    from apps.payments.verifiers import VerificationRequest, VerificationResult, get_verifier

    receipt, rtype = None, "image/jpeg"
    try:
        receipt = private_files.read(cp.file) if cp.file else None
        rtype = (cp.file or {}).get("content_type") or rtype
    except Exception:  # noqa: BLE001
        log.warning("Could not read commission receipt %s", cp.pk)
    acc = cp.paid_to or {}
    req = VerificationRequest(channel=cp.bank, reference=cp.reference, amount=cp.amount,
                              receiver_account=acc.get("account_number", ""), receiver_name=acc.get("account_name", ""),
                              receipt=receipt, receipt_type=rtype,
                              receipt_name="receipt.pdf" if rtype == "application/pdf" else "receipt.jpg")
    try:
        r = get_verifier().verify(req)
    except Exception as exc:  # noqa: BLE001
        r = VerificationResult("ERROR", reason=f"Verifier error: {exc}"[:255])
    return _apply(cp.pk, r)


def _apply(pk: int, r) -> CommissionPayment:
    from apps.payments import references

    with transaction.atomic():
        cp = CommissionPayment.objects.select_for_update().select_related("agency").get(pk=pk)
        if cp.status not in (CP.SUBMITTED,):
            return cp
        now = timezone.now()
        cp.verify_attempts += 1
        cp.bank_check = {"outcome": r.outcome, "reason": r.reason, "amount": str(r.amount) if r.amount is not None else None,
                    "payer": r.payer_name, "transaction_id": r.transaction_id, "checked_at": now.isoformat()}
        if r.outcome in ("ERROR", "PENDING"):
            cp.next_check_at = now + timedelta(minutes=min(2 ** cp.verify_attempts, 60))
            cp.save()
            return cp
        if r.outcome == "UNSUPPORTED":
            cp.status, cp.next_check_at, cp.reason = CP.MANUAL, None, r.reason[:255]
            cp.save()
            return cp
        if r.outcome == "NOT_FOUND":
            return _reject(cp, r.reason or "The bank has no record of this transfer.")
        if r.transaction_id and not cp.reference:
            ref = references.normalise_reference(r.transaction_id)
            try:
                references.claim(cp.bank, ref, source="commission")
            except references.DuplicateReference:
                return _reject(cp, f"This receipt ({ref}) was already used for another payment.")
            cp.reference = ref
        if r.amount is not None and abs(r.amount - cp.amount) > Decimal("0.00"):
            return _reject(cp, f"The bank shows {r.amount:,.2f} ETB, not {cp.amount:,.2f} ETB.")
        _verified(cp, r.amount if r.amount is not None else cp.amount, by=None)
        return cp


def _reject(cp: CommissionPayment, reason: str, by=None) -> CommissionPayment:
    from apps.payments.models import BankReference

    cp.status, cp.reason, cp.next_check_at = CP.REJECTED, reason[:255], None
    cp.save()
    if cp.reference:
        BankReference.objects.filter(bank=cp.bank, reference=cp.reference, payment__isnull=True,
                                     source="commission").delete()
    for m in cp.agency.staff.filter(is_active=True, role__in=[AgencyStaff.Role.OWNER, AgencyStaff.Role.MANAGER]):
        notify(m.user, Notification.Kind.PAYMENT, "Commission receipt rejected", f"{reason} Upload the correct receipt.",
               {"commission_payment": cp.pk})
    return cp


def _verified(cp: CommissionPayment, amount, *, by):
    """Mark commission due as collected, oldest first; whatever is left over stays as credit."""
    cp.status, cp.amount_verified, cp.verified_at, cp.verified_by = CP.VERIFIED, amount, timezone.now(), by
    cp.unallocated = amount  # credit until it is used for commission due
    cp.next_check_at, cp.reason = None, ""
    cp.save()
    allocate(cp.agency, by=by)
    for m in cp.agency.staff.filter(is_active=True, role__in=[AgencyStaff.Role.OWNER, AgencyStaff.Role.MANAGER]):
        notify(m.user, Notification.Kind.PAYMENT, "Commission received ✅",
               f"Nebret received your {amount:,.2f} ETB commission payment. Thank you!", {"commission_payment": cp.pk})


@transaction.atomic
def allocate(agency: Agency, by=None) -> int:
    """Use verified credit for commission due, oldest sale first. Returns how many sales were marked collected."""
    pays = list(agency.commission_payments.select_for_update().filter(status=CP.VERIFIED, unallocated__gt=0)
                .order_by("verified_at"))
    n = 0
    for e in (agency.ledger.select_for_update().filter(payout_status__in=AgencyLedgerEntry.NEBRET_RECEIVABLE)
              .order_by("created_at")):
        pool = sum((p.unallocated for p in pays), ZERO)
        if pool < e.commission:
            break
        need = e.commission
        for p in pays:
            if need <= 0:
                break
            take = min(p.unallocated, need)
            if take > 0:
                p.unallocated -= take
                need -= take
                p.save(update_fields=["unallocated", "updated_at"])
                p.entries.add(e)
        e.payout_status, e.paid_at, e.paid_by = Payout.COLLECTED, timezone.now(), by
        e.paid_reference = ",".join(p.reference for p in pays if p.reference)[:64]
        e.save(update_fields=["payout_status", "paid_at", "paid_by", "paid_reference", "updated_at"])
        n += 1
    return n


def manual_decision(cp: CommissionPayment, staff, *, approve: bool, note: str = "", amount=None) -> CommissionPayment:
    with transaction.atomic():
        cp = CommissionPayment.objects.select_for_update().select_related("agency").get(pk=cp.pk)
        if cp.status not in (CP.SUBMITTED, CP.MANUAL):
            raise BusinessRuleError("This payment was already decided.")
        if approve:
            _verified(cp, Decimal(str(amount)) if amount else cp.amount, by=staff)
        else:
            _reject(cp, note or "Rejected by Nebret.", by=staff)
    return cp


def verify_due(limit: int = 20) -> int:
    ids = list(CommissionPayment.objects.filter(status=CP.SUBMITTED, next_check_at__lte=timezone.now())
               .values_list("pk", flat=True)[:limit])
    for pk in ids:
        verify(CommissionPayment.objects.get(pk=pk))
    return len(ids)


def row(cp: CommissionPayment, request=None) -> dict:
    from apps.core import private_files

    return {"id": cp.pk, "amount": str(cp.amount), "status": cp.status, "status_label": cp.get_status_display(),
            "bank": cp.bank, "reference": cp.display_reference, "transfer_datetime": cp.transfer_datetime,
            "created_at": cp.created_at, "reason": cp.reason, "unallocated": str(cp.unallocated),
            "receipt_url": private_files.signed_url(cp.file, request) if cp.file else ""}
