"""
Partner bank accounts: submit (owner) → approve (Nebret staff) → usable after the cooling-off period.

A company without a usable account can't publish listings, keep them live or take new orders.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.payments.banks import Bank, Currency

from .models import Agency, AgencyStaff, BankAccountEvent, PartnerBankAccount

A = PartnerBankAccount.Status


def _cooling_off() -> timedelta:
    return timedelta(hours=settings.NEBRET["BANK_ACCOUNT_COOLING_OFF_HOURS"])


def _notify_change(account: PartnerBankAccount, title: str, body: str):
    """Every change goes to the company owner(s) and to Nebret staff."""
    from apps.accounts.models import User
    from apps.notifications.models import Notification
    from apps.notifications.services import notify

    owners = [s.user for s in account.agency.staff.filter(role=AgencyStaff.Role.OWNER, is_active=True)
              .select_related("user")]
    for user in owners + list(User.objects.filter(is_staff=True, is_active=True)):
        notify(user, Notification.Kind.PAYMENT, title[:120], body[:500], {"bank_account": account.pk,
                                                                          "agency": account.agency_id})


def _event(account, action, by=None, note="", **data):
    return BankAccountEvent.objects.create(account=account, action=action, by=by, note=note[:255], data=data)


def usable_accounts(agency: Agency, currency: str | None = None):
    qs = agency.bank_accounts.filter(status=A.APPROVED, is_active=True, is_verified=True,
                                     usable_from__lte=timezone.now())
    return qs.filter(currency=currency) if currency else qs


def has_usable_account(agency: Agency | None) -> bool:
    return bool(agency) and usable_accounts(agency).exists()


def account_for_order(agency: Agency, currency: str = "ETB") -> PartnerBankAccount | None:
    """The account a new order is paid into: the newest usable one in the order's currency."""
    return usable_accounts(agency, currency).order_by("-verified_at").first()


def _clean(bank, account_name, account_number, currency, swift_code, branch) -> dict:
    bank = (bank or "").upper()
    if bank not in Bank.values:
        raise BusinessRuleError("Choose one of the supported banks: " + ", ".join(Bank.labels) + ".")
    currency = (currency or "ETB").upper()
    if currency not in Currency.values:
        raise BusinessRuleError("Choose ETB, USD or EUR.")
    number = "".join(ch for ch in str(account_number or "") if ch.isdigit())
    if not 8 <= len(number) <= 20:
        raise BusinessRuleError("Enter the full account number (8–20 digits).")
    if len((account_name or "").strip()) < 3:
        raise BusinessRuleError("Enter the account name exactly as the bank shows it.")
    swift = (swift_code or "").strip().upper()
    if currency != "ETB" and len(swift) not in (8, 11):
        raise BusinessRuleError("USD and EUR accounts need the bank's SWIFT code (8 or 11 characters).")
    return {"bank": bank, "account_name": account_name.strip()[:120], "account_number": number,
            "currency": currency, "swift_code": swift, "branch": (branch or "").strip()[:80]}


@transaction.atomic
def submit_account(agency: Agency, user, *, bank: str, account_name: str, account_number: str, currency: str = "ETB",
                   swift_code: str = "", branch: str = "", replaces: PartnerBankAccount | None = None,
                   ) -> PartnerBankAccount:
    member = agency.staff.filter(user=user, is_active=True).first()
    if not member or member.role != AgencyStaff.Role.OWNER:
        raise BusinessRuleError("Only the company owner can add or change the bank account.")
    data = _clean(bank, account_name, account_number, currency, swift_code, branch)
    if replaces and replaces.agency_id != agency.id:
        raise BusinessRuleError("That account belongs to another company.")
    acc = PartnerBankAccount.objects.create(agency=agency, replaces=replaces, created_by=user, **data)
    _event(acc, "SUBMITTED", user, replaces=replaces.pk if replaces else None, bank=acc.bank,
           number=acc.masked_number, currency=acc.currency)
    _notify_change(acc, "Bank account submitted",
                   f"{agency.name}: new {acc.bank} account {acc.masked_number} "
                   f"({acc.currency}) was submitted by {user.full_name} and is waiting for Nebret approval.")
    return acc


@transaction.atomic
def add_account_by_staff(agency: Agency, staff, *, bank: str, account_name: str, account_number: str,
                         currency: str = "ETB", swift_code: str = "", branch: str = "", note: str = "",
                         ) -> PartnerBankAccount:
    """
    Nebret staff enter the account from the company's signed documents (e.g. when creating the company).

    Recorded and approved in one step. A company's FIRST account is usable at once – there is nothing to redirect
    yet. A change to an existing account replaces the newest one in that currency and still waits out the
    cooling-off period, exactly like an owner's change.
    """
    if not getattr(staff, "is_staff", False):
        raise BusinessRuleError("Only Nebret staff can add a bank account for a company.")
    data = _clean(bank, account_name, account_number, currency, swift_code, branch)
    current = (agency.bank_accounts.select_for_update().filter(status=A.APPROVED, currency=data["currency"])
               .order_by("-verified_at").first())
    if current and current.account_number == data["account_number"] and current.bank == data["bank"]:
        return current
    acc = PartnerBankAccount.objects.create(agency=agency, replaces=current, created_by=staff, **data)
    _event(acc, "SUBMITTED", staff, note or "Entered by Nebret staff", replaces=current.pk if current else None,
           bank=acc.bank, number=acc.masked_number, currency=acc.currency)
    approve_account(acc, staff, note or "Entered by Nebret staff from the company's documents")
    if not agency.bank_accounts.filter(status=A.APPROVED).exclude(pk=acc.pk).exists():
        PartnerBankAccount.objects.filter(pk=acc.pk).update(usable_from=timezone.now())
    acc.refresh_from_db()
    return acc


@transaction.atomic
def approve_account(account: PartnerBankAccount, staff, note: str = "") -> PartnerBankAccount:
    if not getattr(staff, "is_staff", False):
        raise BusinessRuleError("Only Nebret staff can approve bank accounts.")
    account = PartnerBankAccount.objects.select_for_update().get(pk=account.pk)
    if account.status != A.PENDING:
        raise BusinessRuleError("This account is not waiting for approval.")
    now = timezone.now()
    account.status, account.is_verified, account.is_active = A.APPROVED, True, True
    account.verified_by, account.verified_at, account.usable_from = staff, now, now + _cooling_off()
    account.save()
    _event(account, "APPROVED", staff, note, usable_from=account.usable_from.isoformat())
    if account.replaces_id:
        old = account.replaces
        # The old account stays usable until the new one's cooling-off ends, then it is retired by the scheduler.
        _event(old, "REPLACEMENT_APPROVED", staff, f"Replaced by {account.masked_number} from "
                                                   f"{timezone.localtime(account.usable_from):%d %b %H:%M}")
    _notify_change(account, "Bank account approved",
                   f"{account.agency.name}: {account.bank} {account.masked_number} was approved by Nebret and buyers "
                   f"will be told to pay into it from {timezone.localtime(account.usable_from):%d %b %Y %H:%M}.")
    return account


@transaction.atomic
def reject_account(account: PartnerBankAccount, staff, reason: str) -> PartnerBankAccount:
    if not getattr(staff, "is_staff", False):
        raise BusinessRuleError("Only Nebret staff can reject bank accounts.")
    if not reason.strip():
        raise BusinessRuleError("Say why the account is rejected.")
    account = PartnerBankAccount.objects.select_for_update().get(pk=account.pk)
    if account.status != A.PENDING:
        raise BusinessRuleError("This account is not waiting for approval.")
    account.status, account.rejection_reason = A.REJECTED, reason.strip()[:255]
    account.save(update_fields=["status", "rejection_reason", "updated_at"])
    _event(account, "REJECTED", staff, reason)
    _notify_change(account, "Bank account rejected",
                   f"{account.agency.name}: {account.bank} {account.masked_number} was rejected – {reason.strip()}")
    return account


@transaction.atomic
def retire_account(account: PartnerBankAccount, by, note: str = "") -> PartnerBankAccount:
    """Switch an account off (owner or Nebret staff). Orders already placed keep their frozen account details."""
    owner = account.agency.staff.filter(user=by, is_active=True, role=AgencyStaff.Role.OWNER).exists()
    if not (owner or getattr(by, "is_staff", False)):
        raise BusinessRuleError("Only the company owner or Nebret can switch off a bank account.")
    account = PartnerBankAccount.objects.select_for_update().get(pk=account.pk)
    if account.status == A.RETIRED:
        return account
    account.status, account.is_active = A.RETIRED, False
    account.save(update_fields=["status", "is_active", "updated_at"])
    _event(account, "RETIRED", by, note)
    _notify_change(account, "Bank account switched off",
                   f"{account.agency.name}: {account.bank} {account.masked_number} was switched off"
                   + (f" – {note}" if note else "."))
    return account


def retire_replaced_accounts() -> int:
    """Scheduler: once a replacement's cooling-off is over, the account it replaces is retired."""
    n = 0
    for acc in PartnerBankAccount.objects.filter(status=A.APPROVED, usable_from__lte=timezone.now(),
                                                 replaces__status=A.APPROVED).select_related("replaces"):
        old = acc.replaces
        old.status, old.is_active = A.RETIRED, False
        old.save(update_fields=["status", "is_active", "updated_at"])
        _event(old, "RETIRED", None, f"Replaced by {acc.masked_number}")
        n += 1
    return n


def pause_listings_without_account() -> int:
    """Scheduler: live listings of companies with no usable bank account go back to draft."""
    from apps.market.models import Listing

    n = 0
    for agency in Agency.objects.filter(listings__status__in=[Listing.Status.ACTIVE, Listing.Status.SOLD_OUT]).distinct():
        if has_usable_account(agency):
            continue
        n += Listing.objects.filter(company=agency, status__in=[Listing.Status.ACTIVE, Listing.Status.SOLD_OUT]
                                    ).update(status=Listing.Status.DRAFT)
    return n


def account_row(a: PartnerBankAccount, *, full: bool = True) -> dict:
    return {"id": a.id, "bank": a.bank, "bank_name": dict(Bank.choices).get(a.bank, a.bank),
            "account_name": a.account_name, "account_number": a.account_number if full else a.masked_number,
            "masked_number": a.masked_number, "currency": a.currency, "swift_code": a.swift_code, "branch": a.branch,
            "status": a.status, "status_label": a.get_status_display(), "is_usable": a.is_usable,
            "usable_from": a.usable_from, "verified_at": a.verified_at, "rejection_reason": a.rejection_reason,
            "replaces": a.replaces_id, "created_at": a.created_at,
            "history": [{"action": e.action, "note": e.note, "at": e.created_at,
                         "by": e.by.full_name if e.by_id else ""} for e in a.events.select_related("by")[:20]]}
