"""
Marketplace bank-transfer payments.

Every marketplace payment – any amount – is a bank transfer straight into the seller's own account. Nebret never holds
the money. A payment counts only after three independent checks:

1. **Receipt** – the buyer uploads the receipt with the bank, reference, amount, date/time and payer name
   (:func:`submit_proof`). One ``PaymentTransaction`` (provider ``bank``, PENDING) is created per receipt and the
   reference is claimed so it can never be used twice anywhere on Nebret.
2. **Bank** – ShegerPay checks the transfer in the background (:func:`verify_due_proofs`, run by the scheduler, never
   inside an HTTP request). It must exist, be complete, credit the order's frozen account, match the claimed amount
   exactly, come from the buyer or a declared co-payer, fall in the order's time window and (when the bank returns the
   narration) carry the order's payment code.
3. **Seller** – an owner or manager of the company types what their bank statement shows (amount, payer, time,
   statement reference). It must match exactly (:func:`confirm_proof`). Above ``DUAL_CONTROL_THRESHOLD`` a second,
   different owner/manager must confirm too.

Only then is the payment applied – through ``payments.services.apply_status`` → registry → ``orders.apply_payment``,
the same single path every payment takes, so fulfilment, notifications and commission run exactly once.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.core.business_days import add_business_days, business_days_between
from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify
from apps.payments.banks import Channel, auto_verifiable

from ..models import BankTransferProof, Installment, ListingUnit, Order, OrderEvent, PartnerRefund, money
from .orders import fmt, log_event

log = logging.getLogger(__name__)
P = BankTransferProof.Status
ZERO = Decimal("0")


# =========================================================================== settings helpers
def _cfg(key):
    return settings.NEBRET[key]


def time_tolerance() -> timedelta:
    return timedelta(minutes=_cfg("TRANSFER_TIME_TOLERANCE_MINUTES"))


def dual_control_threshold() -> Decimal:
    return Decimal(str(_cfg("DUAL_CONTROL_THRESHOLD")))


def amount_tolerance() -> Decimal:
    return Decimal(str(_cfg("AMOUNT_TOLERANCE_ETB")))


def support_phone() -> str:
    return _cfg("SUPPORT_PHONE")


# =========================================================================== matching helpers
def _name_tokens(name: str) -> list[str]:
    text = unicodedata.normalize("NFKD", name or "").lower()
    return [t for t in re.split(r"[^\w]+", text) if t and not t.isdigit()]


def names_match(a: str, b: str) -> bool:
    """Bank receipts truncate and reorder names: every word of the shorter name must appear in the longer one."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) == 1 and len(long_) > 1:
        return False  # one word ("Abebe") is too weak to identify a payer
    return all(t in long_ for t in short)


def accounts_match(seen: str, expected: str) -> bool:
    """Exact digits, or a masked number from a receipt (``1000****3456``) whose visible digits all line up."""
    seen = re.sub(r"[^0-9*xX•]", "", seen or "")
    expected = re.sub(r"\D", "", expected or "")
    if not seen or not expected:
        return False
    if seen.isdigit():
        return seen == expected
    if len(seen) != len(expected):
        head = re.match(r"^\d*", seen).group(0)
        tail = re.search(r"\d*$", seen).group(0)
        return len(head) + len(tail) >= 4 and expected.startswith(head) and expected.endswith(tail)
    return sum(c.isdigit() for c in seen) >= 4 and all(c == e for c, e in zip(seen, expected) if c.isdigit())


def mask_account(number: str, keep: int = 5) -> str:
    digits = re.sub(r"\D", "", number or "")
    return ("•" * max(len(digits) - keep, 0)) + digits[-keep:] if digits else ""


def allowed_payers(order: Order) -> list[str]:
    return [n for n in [order.buyer_name, order.user.full_name, *(order.co_payers or [])] if n]


# =========================================================================== who may do what
def staff_member(user, order: Order):
    if not order.company_id or not getattr(user, "is_authenticated", False):
        return None
    from apps.agencies.models import AgencyStaff

    return AgencyStaff.objects.filter(user=user, agency_id=order.company_id, is_active=True).first()


def can_view(user, order: Order) -> bool:
    """Receipts and agreements: the buyer, the order's company staff and Nebret staff."""
    return bool(getattr(user, "is_authenticated", False)) and (
        order.user_id == user.id or user.is_staff or staff_member(user, order) is not None)


def confirmer_role(user, order: Order) -> str:
    """``partner`` (owner / manager of the seller) or ``nebret`` (staff, on the seller's behalf). Agents can't."""
    member = staff_member(user, order)
    if member and member.can_manage:
        return "partner"
    if getattr(user, "is_staff", False):
        return "nebret"
    if member:
        raise BusinessRuleError("Only the company owner or a manager can confirm or reject payments.")
    raise BusinessRuleError("You can't confirm payments for this order.")


def _managers(order: Order):
    from apps.agencies.models import AgencyStaff

    if not order.company_id:
        from apps.accounts.models import User

        return list(User.objects.filter(is_staff=True, is_active=True))
    return [s.user for s in AgencyStaff.objects.filter(agency_id=order.company_id, is_active=True,
                                                         role__in=[AgencyStaff.Role.OWNER, AgencyStaff.Role.MANAGER])
            .select_related("user")]


def _notify_buyer(order: Order, title: str, body: str, proof=None):
    data = {"order": order.reference, **({"proof": proof.pk} if proof else {})}
    transaction.on_commit(lambda: notify(order.user, Notification.Kind.PROOF, title[:120], body[:500], data))


def _notify_managers(order: Order, title: str, body: str, proof=None):
    data = {"order": order.reference, "company_order": order.reference, **({"proof": proof.pk} if proof else {})}
    users = _managers(order)
    transaction.on_commit(lambda: [notify(u, Notification.Kind.PROOF, title[:120], body[:500], data) for u in users])


def _alert(kind: str, message: str, *, order: Order | None = None, proof=None, user=None, **data):
    from apps.accounts.models import User
    from apps.payments.models import FraudAlert

    alert = FraudAlert.objects.create(kind=kind, message=message[:255], user=user,
                                      agency_id=order.company_id if order else None,
                                      order_reference=order.reference if order else "",
                                      proof_id=proof.pk if proof else None, data=data)
    for staff in User.objects.filter(is_staff=True, is_active=True):
        notify(staff, Notification.Kind.PAYMENT, f"Nebret alert: {alert.get_kind_display()}"[:120], message[:500],
               {"fraud_alert": alert.pk, **({"order": order.reference} if order else {})})
    return alert


# =========================================================================== order set-up
def pay_to_account(order: Order) -> dict:
    """The account a new order is paid into: the seller's newest usable account, or Nebret's for its own listings."""
    if order.company_id:
        from apps.agencies.bank_accounts import account_for_order

        acc = account_for_order(order.company, order.currency)
        if not acc:
            raise BusinessRuleError("This seller can't take payments right now (no approved bank account). "
                                    "Please try again later or contact Nebret support.")
        return acc.snapshot()
    house = settings.NEBRET.get("HOUSE_BANK_ACCOUNT") or {}
    if not house.get("account_number"):
        raise BusinessRuleError("This item can't be ordered right now. Please contact Nebret support.")
    from apps.payments.banks import Bank

    return {"account_id": None, **house, "bank_name": dict(Bank.choices).get(house.get("bank"), house.get("bank")),
            "agency_id": None, "agency_name": "Nebret"}


def prepare_bank_transfer(order: Order) -> Order:
    """Give an order its payment code and frozen account (new orders, and old open orders switching to transfers)."""
    from ..models import new_payment_code

    if order.is_bank_transfer and order.payment_code and order.pay_to_account:
        return order
    order.pay_to_account = order.pay_to_account or pay_to_account(order)
    order.payment_code = order.payment_code or new_payment_code()
    order.payment_method = Order.PaymentMethod.BANK_TRANSFER
    order.save(update_fields=["pay_to_account", "payment_code", "payment_method", "updated_at"])
    return order


def open_proofs(order: Order):
    return order.proofs.filter(status__in=BankTransferProof.OPEN_STATUSES)


def amount_under_review(order: Order) -> Decimal:
    return sum((p.amount_claimed for p in open_proofs(order)), ZERO)


def bank_instructions(order: Order) -> dict:
    if order.status not in (Order.Status.PENDING_PAYMENT, Order.Status.RESERVED, Order.Status.INSTALLMENTS):
        raise BusinessRuleError("Nothing is due on this order.")
    if not order.is_bank_transfer:
        order = prepare_bank_transfer(order)
    acc = order.pay_to_account
    in_review = amount_under_review(order)
    due_now = order.amount_due_now
    deadline = order.expires_at if order.status == Order.Status.PENDING_PAYMENT else None
    if order.status == Order.Status.RESERVED and order.reserved_until:
        deadline = timezone.make_aware(datetime.combine(order.reserved_until, datetime.max.time().replace(microsecond=0)))
    elif order.status == Order.Status.INSTALLMENTS and order.next_installment:
        deadline = timezone.make_aware(datetime.combine(order.next_installment.due_date,
                                                        datetime.max.time().replace(microsecond=0)))
    return {
        "order": order.reference, "status": order.status, "payment_code": order.payment_code,
        "account": {k: acc.get(k, "") for k in ("bank", "bank_name", "account_name", "account_number", "currency",
                                                "swift_code", "branch")},
        "seller": acc.get("agency_name") or (order.company.name if order.company_id else "Nebret"),
        "amount_due_now": str(due_now), "balance_due": str(order.balance_due), "amount_under_review": str(in_review),
        "currency": acc.get("currency") or order.currency, "deadline": deadline,
        "installment_options": [], "support_phone": support_phone(),
        "instructions": [
            f"Transfer exactly {fmt(due_now)} {acc.get('currency') or order.currency} to the account above. "
            "You can split it into several transfers.",
            f"Write the payment code {order.payment_code} in the transfer reason / narration.",
            "Upload the receipt in the app right after the transfer. The bank and the seller confirm it.",
            "Nebret will never send account changes by phone, SMS, Telegram or chat. Only pay into the account "
            "shown here.",
        ],
    }


# =========================================================================== 1. submit
def submit_proof(order: Order, user, *, file, bank: str, reference: str, amount, transfer_datetime, payer_name: str,
                 payer_account: str = "", installment_id=None, declare_co_payer: bool = False) -> BankTransferProof:
    """
    A buyer (or the seller's staff / Nebret staff on their behalf) uploads a receipt. A reused reference is refused
    and raises a fraud alert that survives the rollback.
    """
    from apps.payments import references
    from apps.core import private_files

    data, content_type = private_files.validate(file)
    try:
        with transaction.atomic():
            proof = _submit(order, user, data=data, content_type=content_type, filename=getattr(file, "name", ""),
                            bank=bank, reference=reference, amount=amount, transfer_datetime=transfer_datetime,
                            payer_name=payer_name, payer_account=payer_account, installment_id=installment_id,
                            declare_co_payer=declare_co_payer)
    except references.DuplicateReference as exc:
        references.raise_alert(exc, user=user, agency=order.company if order.company_id else None,
                               order_reference=order.reference, amount=amount,
                               context=f"receipt uploaded by {getattr(user, 'full_name', '') or user}")
        raise
    return proof


def _submit(order, user, *, data, content_type, filename, bank, reference, amount, transfer_datetime, payer_name,
            payer_account, installment_id, declare_co_payer):
    from apps.core import private_files
    from apps.payments import references
    from apps.payments.models import PaymentTransaction

    order = Order.objects.select_for_update(of=("self",)).select_related("user", "company", "listing").get(pk=order.pk)
    on_behalf = order.user_id != user.id
    if on_behalf and not (getattr(user, "is_staff", False) or staff_member(user, order)):
        raise BusinessRuleError("You can only upload receipts for your own order.")
    if order.status == Order.Status.PENDING_PAYMENT:
        if not (order.is_payable or order.amount_paid > 0 or open_proofs(order).exists()):
            raise BusinessRuleError("The hold on this order has ended. Place a new order.")
    elif order.status not in (Order.Status.RESERVED, Order.Status.INSTALLMENTS):
        raise BusinessRuleError("Nothing is due on this order.")
    if order.funding_source != Order.Funding.DIRECT and order.plan == Order.Plan.EQUB_PAYOUT and \
            order.payment_method == Order.PaymentMethod.EQUB:
        raise BusinessRuleError("This order is paid by its Equb.")
    if not order.is_bank_transfer:
        order = prepare_bank_transfer(order)

    bank = (bank or "").upper()
    if bank not in Channel.values:
        raise BusinessRuleError("Choose the bank or wallet the money was sent from.")
    ref = references.normalise_reference(reference)
    if len(ref) < 6:
        raise BusinessRuleError("Enter the transaction reference from the receipt (e.g. FT24123ABC45).")
    amount = money(amount)
    if amount <= 0:
        raise BusinessRuleError("Enter the amount you transferred.")
    left = order.balance_due - amount_under_review(order)
    if amount > left:
        raise BusinessRuleError(f"Only {fmt(max(left, ZERO))} ETB is left to pay (receipts already under review "
                                "are counted).")
    if isinstance(transfer_datetime, str):
        from django.utils.dateparse import parse_datetime

        transfer_datetime = parse_datetime(transfer_datetime)
    if not transfer_datetime:
        raise BusinessRuleError("Enter the date and time of the transfer.")
    if timezone.is_naive(transfer_datetime):
        transfer_datetime = timezone.make_aware(transfer_datetime)
    now = timezone.now()
    if transfer_datetime > now + time_tolerance():
        raise BusinessRuleError("The transfer time is in the future.")
    if transfer_datetime < order.created_at - time_tolerance():
        raise BusinessRuleError("This transfer was made before the order was placed – it can't be used for it.")
    payer_name = " ".join((payer_name or "").split())[:120]
    if len(payer_name) < 3:
        raise BusinessRuleError("Enter the payer's full name as it appears on the receipt.")
    if not any(names_match(payer_name, n) for n in allowed_payers(order)):
        if not declare_co_payer:
            raise BusinessRuleError(f"{payer_name} is not the buyer. If someone paid for you, confirm them as a "
                                    "co-payer and upload again.")
        order.co_payers = [*(order.co_payers or []), payer_name]
        order.save(update_fields=["co_payers", "updated_at"])
        log_event(order, OrderEvent.Kind.NOTE, f"{payer_name} declared as a co-payer.", by=user)
    installment = None
    if installment_id:
        installment = Installment.objects.filter(pk=installment_id, order=order).first()
        if not installment:
            raise BusinessRuleError("That installment is not part of this order.")
    currency = order.pay_to_account.get("currency") or order.currency

    references.check_unused(bank, ref)
    key = private_files.store(data, content_type, folder=f"receipts/{order.reference}", name=filename)
    purpose = (PaymentTransaction.Purpose.INSTALLMENT if order.status == Order.Status.INSTALLMENTS
               else PaymentTransaction.Purpose.ORDER)
    txn = PaymentTransaction.objects.create(
        user=order.user, provider=PaymentTransaction.Provider.BANK, purpose=purpose, amount=amount, currency=currency,
        status=PaymentTransaction.Status.PENDING, target=order, provider_reference=ref,
        payer_phone=order.contact_phone, agency=order.company,
        platform_fee=order.company.commission_on(amount) if order.company_id else ZERO,
        recorded_by=user if on_behalf else None,
        raw_request={"bank": bank, "reference_as_typed": reference[:128], "payment_code": order.payment_code})
    references.claim(bank, ref, payment=txn, source="receipt")
    proof = BankTransferProof.objects.create(
        order=order, installment=installment, payment_transaction=txn, submitted_by=user, bank=bank,
        reference_as_typed=reference.strip()[:128], normalised_reference=ref, payer_name=payer_name,
        payer_account=mask_account(payer_account), currency=currency, amount_claimed=amount,
        transfer_datetime=transfer_datetime, file=key, status=P.SUBMITTED, next_check_at=now,
        requires_manual_review=not auto_verifiable(bank, currency))
    # Keep the unit while the receipt is checked.
    if order.status == Order.Status.PENDING_PAYMENT:
        until = max(order.expires_at or now, add_business_days(now, _cfg("BANK_HOLD_BUSINESS_DAYS")))
        order.expires_at = until
        order.save(update_fields=["expires_at", "updated_at"])
        if order.unit_id:
            ListingUnit.objects.filter(pk=order.unit_id).update(held_until=until)
    log_event(order, OrderEvent.Kind.PROOF_SUBMITTED,
              f"Receipt {bank} {ref} for {fmt(amount)} {currency} from {payer_name}"
              + (f" (uploaded by {user.full_name})" if on_behalf else "") + ".", by=user)
    if proof.requires_manual_review:
        _alert("MANUAL_REVIEW", f"{order.reference}: {bank} receipt {ref} ({fmt(amount)} {currency}) can't be "
                                f"verified automatically – please check it.", order=order, proof=proof)
    _notify_managers(order, "New payment receipt",
                     f"{order.reference}: {fmt(amount)} {currency} receipt from {payer_name}. It is being checked "
                     "with the bank – you'll be asked to confirm it.", proof)
    return proof


# =========================================================================== 2. verify (scheduler)
def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(2 ** max(attempts - 1, 0), 60))


def verify_due_proofs(limit: int = 50) -> dict:
    now = timezone.now()
    ids = list(BankTransferProof.objects.filter(status__in=[P.SUBMITTED, P.VERIFYING], requires_manual_review=False)
               .filter(next_check_at__lte=now).order_by("next_check_at").values_list("pk", flat=True)[:limit])
    out = {"verified": 0, "rejected": 0, "waiting": 0}
    for pk in ids:
        result = verify_proof(BankTransferProof.objects.get(pk=pk))
        out[{"VERIFIED_AWAITING_PARTNER": "verified", "REJECTED": "rejected"}.get(result.status, "waiting")] += 1
    out["escalated"] = escalate_stuck_verifications()
    return out


def verify_proof(proof: BankTransferProof) -> BankTransferProof:
    """Ask the bank (ShegerPay) about one receipt – the network call happens outside any transaction."""
    from apps.payments.verifiers import VerificationRequest, get_verifier

    order = proof.order
    acc = order.pay_to_account or {}
    req = VerificationRequest(channel=proof.bank, reference=proof.normalised_reference, amount=proof.amount_claimed,
                              receiver_account=acc.get("account_number", ""),
                              receiver_name=acc.get("account_name", ""),
                              payer_account=re.sub(r"\D", "", proof.payer_account), currency=proof.currency)
    try:
        result = get_verifier().verify(req)
    except Exception as exc:  # noqa: BLE001 – a verifier bug must not stop the scheduler
        log.exception("Verifier crashed for proof %s", proof.pk)
        from apps.payments.verifiers import VerificationResult

        result = VerificationResult("ERROR", reason=f"Verifier error: {exc}"[:255])
    return _apply_verification(proof.pk, result)


def _apply_verification(proof_id: int, result) -> BankTransferProof:
    with transaction.atomic():
        proof = BankTransferProof.objects.select_for_update().select_related("order__user").get(pk=proof_id)
        if proof.status not in (P.SUBMITTED, P.VERIFYING):
            return proof
        order = proof.order
        now = timezone.now()
        proof.status = P.VERIFYING
        proof.verify_attempts += 1
        proof.shegerpay_checked_at = now
        proof.shegerpay_request = result.request or proof.shegerpay_request
        proof.shegerpay_response = {"outcome": result.outcome, "reason": result.reason,
                                    "amount": str(result.amount) if result.amount is not None else None,
                                    "payer": result.payer_name, "receiver_account": result.receiver_account,
                                    "receiver_name": result.receiver_name, "narration": result.narration,
                                    "transferred_at": result.transferred_at.isoformat() if result.transferred_at else None,
                                    "transaction_id": result.transaction_id, "raw": result.raw}
        if result.outcome in ("ERROR", "PENDING"):
            proof.next_check_at = now + _backoff(proof.verify_attempts)
            proof.save()
            return proof
        if result.outcome == "UNSUPPORTED":
            proof.requires_manual_review = True
            proof.next_check_at = None
            proof.save()
            _alert("MANUAL_REVIEW", f"{order.reference}: {proof.bank} receipt {proof.normalised_reference} can't be "
                                    f"verified automatically – please check it.", order=order, proof=proof)
            return proof
        if result.outcome == "NOT_FOUND":
            proof.save()
            return _reject(proof, result.reason or "The bank has no record of this transfer.", by=None)

        problems, manual = _check_facts(proof, order, result)
        if problems:
            proof.save()
            return _reject(proof, " ".join(problems), by=None)
        if manual:
            proof.requires_manual_review = True
            proof.next_check_at = None
            proof.save()
            _alert("MANUAL_REVIEW", f"{order.reference}: receipt {proof.normalised_reference} – {manual}",
                   order=order, proof=proof)
            return proof
        _mark_verified(proof, result.amount, by=None)
        return proof


def _check_facts(proof: BankTransferProof, order: Order, r) -> tuple[list[str], str]:
    """(reasons to reject, reason to send to manual review instead)."""
    from apps.payments.references import normalise_reference

    problems, manual = [], ""
    acc = order.pay_to_account or {}
    if r.transaction_id and normalise_reference(r.transaction_id) != proof.normalised_reference:
        problems.append("The bank returned a different transaction reference.")
    if r.amount is None:
        manual = "the bank did not return the amount."
    elif abs(r.amount - proof.amount_claimed) > amount_tolerance():
        problems.append(f"The bank shows {fmt(r.amount)} ETB, not {fmt(proof.amount_claimed)} ETB.")
    if r.receiver_account:
        if not accounts_match(r.receiver_account, acc.get("account_number", "")):
            problems.append("The money was not sent to the seller's account shown in the app.")
    elif settings.NEBRET.get("SHEGERPAY_RECEIVER_CHECK") == "merchant_name":
        if r.receiver_name and not names_match(r.receiver_name, acc.get("account_name", "")):
            problems.append("The money was not sent to the seller's account shown in the app.")
    else:
        manual = manual or "the bank did not return the receiving account."
    if r.payer_name:
        if not any(names_match(r.payer_name, n) for n in allowed_payers(order)):
            problems.append(f"The transfer was made by {r.payer_name}, who is not the buyer or a declared co-payer.")
    else:
        manual = manual or "the bank did not return the payer's name."
    window_start = order.created_at - time_tolerance()
    if r.transferred_at:
        when = r.transferred_at if timezone.is_aware(r.transferred_at) else timezone.make_aware(r.transferred_at)
        if when < window_start or when > timezone.now() + time_tolerance():
            problems.append("The transfer was made outside this order's payment window.")
        elif abs(when - proof.transfer_datetime) > time_tolerance():
            problems.append("The transfer time on the receipt doesn't match the bank.")
    if r.narration and order.payment_code and order.payment_code not in normalise_reference(r.narration):
        problems.append(f"The payment code {order.payment_code} is missing from the transfer reason.")
    return problems, manual


def _mark_verified(proof: BankTransferProof, amount, *, by=None, note: str = ""):
    order = proof.order
    proof.status = P.VERIFIED_AWAITING_PARTNER
    proof.amount_verified = money(amount if amount is not None else proof.amount_claimed)
    proof.verified_at = timezone.now()
    proof.verified_by = by
    proof.requires_manual_review = proof.requires_manual_review and by is None
    proof.next_check_at = None
    proof.save()
    log_event(order, OrderEvent.Kind.PROOF_VERIFIED,
              f"Receipt {proof.normalised_reference} verified {'by Nebret' if by else 'with the bank'}: "
              f"{fmt(proof.amount_verified)} {proof.currency}." + (f" {note}" if note else ""), by=by)
    _notify_buyer(order, "Receipt verified ✅", f"Your transfer of {fmt(proof.amount_verified)} {proof.currency} for "
                                              f"{order.reference} was verified. Waiting for the seller to confirm.",
                  proof)
    _notify_managers(order, "Payment to confirm",
                     f"{order.reference}: {fmt(proof.amount_verified)} {proof.currency} from {proof.payer_name} "
                     "was verified with the bank. Check your statement and confirm it.", proof)


def _reject(proof: BankTransferProof, reason: str, *, by=None) -> BankTransferProof:
    """Rejected receipts free their reference (the buyer may correct and upload again) – nothing was applied."""
    from apps.payments.models import BankReference, PaymentTransaction
    from apps.payments.services import apply_status

    order = proof.order
    proof.status = P.REJECTED
    proof.rejection_reason = reason[:255]
    proof.next_check_at = None
    proof.save()
    apply_status(proof.payment_transaction_id, PaymentTransaction.Status.FAILED, reason=f"Receipt rejected: {reason}")
    BankReference.objects.filter(payment_id=proof.payment_transaction_id).delete()
    log_event(order, OrderEvent.Kind.PROOF_REJECTED, f"Receipt {proof.normalised_reference} rejected: {reason}", by=by)
    _notify_buyer(order, "Receipt rejected", f"Your receipt {proof.reference_as_typed} for {order.reference} was "
                                             f"rejected: {reason}", proof)
    _check_repeated_rejections(order)
    return proof


def _check_repeated_rejections(order: Order):
    limit = _cfg("REPEATED_REJECTIONS_ALERT")
    since = timezone.now() - timedelta(days=30)
    n = BankTransferProof.objects.filter(order__user=order.user, status=P.REJECTED, updated_at__gte=since).count()
    if n == limit:
        _alert("REPEATED_REJECTIONS", f"{order.user.full_name}: {n} receipts rejected in 30 days (latest on "
                                      f"{order.reference}).", order=order, user=order.user)


def escalate_stuck_verifications() -> int:
    hours = _cfg("VERIFIER_ESCALATE_HOURS")
    cutoff = timezone.now() - timedelta(hours=hours)
    n = 0
    for proof in BankTransferProof.objects.filter(status__in=[P.SUBMITTED, P.VERIFYING], requires_manual_review=False,
                                                  created_at__lt=cutoff, escalated_at__isnull=True
                                                  ).select_related("order"):
        BankTransferProof.objects.filter(pk=proof.pk).update(escalated_at=timezone.now())
        _alert("VERIFIER_OUTAGE", f"{proof.order.reference}: receipt {proof.normalised_reference} still not verified "
                                  f"after {hours} h ({(proof.shegerpay_response or {}).get('reason') or 'no answer'}).",
               order=proof.order, proof=proof)
        n += 1
    return n


@transaction.atomic
def manual_review(proof: BankTransferProof, staff, *, approve: bool, note: str, amount=None) -> BankTransferProof:
    """Nebret staff check a receipt ShegerPay can't (another bank, USD/EUR, an RTGS slip…)."""
    if not getattr(staff, "is_staff", False):
        raise BusinessRuleError("Only Nebret staff can review receipts.")
    if not note.strip():
        raise BusinessRuleError("Write what you checked.")
    proof = BankTransferProof.objects.select_for_update().get(pk=proof.pk)
    if proof.status not in (P.SUBMITTED, P.VERIFYING):
        raise BusinessRuleError("This receipt is not waiting for review.")
    if not approve:
        return _reject(proof, note.strip(), by=staff)
    amount = money(amount) if amount not in (None, "") else proof.amount_claimed
    if amount != proof.amount_claimed:
        raise BusinessRuleError("The verified amount must equal the amount on the receipt (exact amounts only).")
    _mark_verified(proof, amount, by=staff, note=note.strip())
    return proof


# =========================================================================== 3. seller confirms
def confirm_proof(proof: BankTransferProof, user, *, amount, payer_name: str, credited_at, statement_ref: str,
                  note: str = "", evidence=None) -> BankTransferProof:
    """
    The seller confirms what its bank statement shows. Exact amount, same payer, time within tolerance – otherwise the
    receipt is DISPUTED and goes to Nebret. Applied only after the final confirmation (two people above the threshold).
    """
    order = proof.order
    role = confirmer_role(user, order)
    evidence_key = {}
    if role == "nebret" and order.company_id:
        if not note.strip():
            raise BusinessRuleError("Write why Nebret is confirming for the seller.")
        if evidence is None:
            raise BusinessRuleError("Attach the evidence (the seller's statement or written confirmation).")
        from apps.core import private_files

        data, ctype = private_files.validate(evidence)
        evidence_key = private_files.store(data, ctype, folder=f"evidence/{order.reference}",
                                           name=getattr(evidence, "name", ""))
    amount = money(amount)
    statement_ref = (statement_ref or "").strip()[:64]
    if not statement_ref:
        raise BusinessRuleError("Enter the reference from your bank statement.")
    if isinstance(credited_at, str):
        from django.utils.dateparse import parse_datetime

        credited_at = parse_datetime(credited_at)
    if not credited_at:
        raise BusinessRuleError("Enter the date and time the money was credited.")
    if timezone.is_naive(credited_at):
        credited_at = timezone.make_aware(credited_at)
    payer_name = " ".join((payer_name or "").split())[:120]

    with transaction.atomic():
        order = Order.objects.select_for_update(of=("self",)).get(pk=order.pk)  # serialises confirmations per order
        proof = BankTransferProof.objects.select_for_update().get(pk=proof.pk)
        if proof.status == P.CONFIRMED:
            raise BusinessRuleError("This payment is already confirmed.")
        allowed = (P.VERIFIED_AWAITING_PARTNER, P.DISPUTED) if role == "nebret" else (P.VERIFIED_AWAITING_PARTNER,)
        if proof.status not in allowed:
            raise BusinessRuleError("This receipt is not waiting for confirmation.")

        mismatches = []
        if abs(amount - (proof.amount_verified or ZERO)) > amount_tolerance():
            mismatches.append(f"amount {fmt(amount)} ≠ {fmt(proof.amount_verified)}")
        if not names_match(payer_name, proof.payer_name):
            mismatches.append(f"payer “{payer_name}” ≠ “{proof.payer_name}”")
        if abs(credited_at - proof.transfer_datetime) > time_tolerance():
            mismatches.append(f"credited {timezone.localtime(credited_at):%d %b %H:%M} vs transfer "
                              f"{timezone.localtime(proof.transfer_datetime):%d %b %H:%M}")
        if mismatches:
            proof.status = P.DISPUTED
            proof.dispute_reason = ("Statement doesn't match: " + "; ".join(mismatches))[:255]
            proof.partner_confirmed_amount, proof.partner_confirmed_payer = amount, payer_name
            proof.partner_confirmed_datetime, proof.partner_statement_ref = credited_at, statement_ref
            proof.save()
            log_event(order, OrderEvent.Kind.DISPUTE, f"Receipt {proof.normalised_reference}: {proof.dispute_reason}",
                      by=user)
            _alert("DISPUTE", f"{order.reference}: {proof.dispute_reason}", order=order, proof=proof, user=user)
            _notify_buyer(order, "Payment being checked",
                          f"The seller's statement doesn't match your receipt for {order.reference}. Nebret is checking "
                          "it and will contact you in the app.", proof)
            return proof

        first = proof.partner_confirmed_by_id is not None and proof.partner_confirmed_at is not None
        needs_two = proof.amount_verified >= dual_control_threshold()
        if first and proof.partner_confirmed_by_id == user.id:
            raise BusinessRuleError("A different owner or manager must give the second confirmation.")
        if first and needs_two:
            if (amount != proof.partner_confirmed_amount
                    or statement_ref.upper() != proof.partner_statement_ref.upper()):
                raise BusinessRuleError("The second confirmation must show the same amount and statement reference.")
            proof.second_confirmer, proof.second_confirmed_at = user, timezone.now()
        else:
            proof.partner_confirmed_by, proof.partner_confirmed_at = user, timezone.now()
            proof.partner_confirmed_amount, proof.partner_confirmed_payer = amount, payer_name
            proof.partner_confirmed_datetime, proof.partner_statement_ref = credited_at, statement_ref
        if role == "nebret":
            proof.on_behalf_note = note.strip()[:255] or proof.on_behalf_note
            proof.on_behalf_evidence = evidence_key or proof.on_behalf_evidence
        if needs_two and not proof.second_confirmer_id:
            proof.save()
            log_event(order, OrderEvent.Kind.NOTE, f"First confirmation of {fmt(amount)} ETB by {user.full_name} – "
                                                   "a second owner/manager must confirm.", by=user)
            _notify_managers(order, "Second confirmation needed",
                             f"{order.reference}: {user.full_name} confirmed {fmt(amount)} ETB. Above "
                             f"{fmt(dual_control_threshold())} ETB a second owner or manager must confirm too.", proof)
            return proof
        return _finalize(proof, order, user, role)


def _finalize(proof: BankTransferProof, order: Order, user, role: str) -> BankTransferProof:
    from apps.payments.models import PaymentTransaction
    from apps.payments.services import apply_status

    amount = proof.partner_confirmed_amount
    proof.status = P.CONFIRMED
    proof.save()
    # The transaction amount is what the seller confirmed (partial / split transfers are normal here).
    PaymentTransaction.objects.filter(pk=proof.payment_transaction_id).update(amount=amount)
    who = f"{user.full_name}" + (" for Nebret on the seller's behalf" if role == "nebret" and order.company_id else "")
    if proof.second_confirmer_id:
        who += f" and {proof.second_confirmer.full_name}"
    log_event(order, OrderEvent.Kind.PARTNER_CONFIRMED,
              f"{fmt(amount)} {proof.currency} confirmed by {who} (statement {proof.partner_statement_ref}).", by=user)
    apply_status(proof.payment_transaction_id, PaymentTransaction.Status.SUCCESS, amount=amount,
                 provider_reference=proof.normalised_reference)
    from .agreement import schedule_issue

    schedule_issue(order)
    return proof


@transaction.atomic
def reject_proof(proof: BankTransferProof, user, reason: str) -> BankTransferProof:
    order = proof.order
    role = confirmer_role(user, order)
    if not (reason or "").strip():
        raise BusinessRuleError("Say why the payment is rejected.")
    proof = BankTransferProof.objects.select_for_update().get(pk=proof.pk)
    allowed = (P.VERIFIED_AWAITING_PARTNER,) if role == "partner" else (
        P.SUBMITTED, P.VERIFYING, P.VERIFIED_AWAITING_PARTNER, P.DISPUTED)
    if proof.status not in allowed:
        raise BusinessRuleError("This receipt can't be rejected now.")
    return _reject(proof, reason.strip(), by=user)


@transaction.atomic
def dispute_proof(proof: BankTransferProof, user, reason: str) -> BankTransferProof:
    order = proof.order
    confirmer_role(user, order)
    if not (reason or "").strip():
        raise BusinessRuleError("Say what is wrong with this payment.")
    proof = BankTransferProof.objects.select_for_update().get(pk=proof.pk)
    if proof.status != P.VERIFIED_AWAITING_PARTNER:
        raise BusinessRuleError("Only a receipt waiting for confirmation can be disputed.")
    proof.status, proof.dispute_reason = P.DISPUTED, reason.strip()[:255]
    proof.save()
    log_event(order, OrderEvent.Kind.DISPUTE, f"Receipt {proof.normalised_reference} disputed: {reason}", by=user)
    _alert("DISPUTE", f"{order.reference}: seller disputes receipt {proof.normalised_reference} – {reason}",
           order=order, proof=proof, user=user)
    _notify_buyer(order, "Payment being checked", f"The seller raised a question about your transfer for "
                                                  f"{order.reference}. Nebret is checking it.", proof)
    return proof


def escalate_overdue_confirmations() -> int:
    """Receipts the seller hasn't confirmed within the SLA (business days, holidays skipped)."""
    sla = _cfg("PARTNER_CONFIRM_SLA_BUSINESS_DAYS")
    now = timezone.now()
    n = 0
    for proof in BankTransferProof.objects.filter(status=P.VERIFIED_AWAITING_PARTNER, sla_escalated_at__isnull=True,
                                                  verified_at__isnull=False).select_related("order"):
        if business_days_between(proof.verified_at, now) < sla:
            continue
        BankTransferProof.objects.filter(pk=proof.pk).update(sla_escalated_at=now)
        order = proof.order
        from apps.agencies.models import AgencyStaff

        owners = [s.user for s in AgencyStaff.objects.filter(agency_id=order.company_id, role=AgencyStaff.Role.OWNER,
                                                             is_active=True).select_related("user")]
        for u in owners:
            notify(u, Notification.Kind.PROOF, "Payment waiting for your confirmation",
                   f"{order.reference}: {fmt(proof.amount_verified)} ETB from {proof.payer_name} has been waiting "
                   f"{sla}+ business days. Confirm or reject it today.", {"order": order.reference, "proof": proof.pk})
        _alert("PARTNER_SLA", f"{order.reference}: receipt {proof.normalised_reference} unconfirmed for {sla}+ "
                              "business days.", order=order, proof=proof)
        n += 1
    return n


def expire_open_proofs(order: Order, reason: str, by=None) -> int:
    """The order was cancelled: unsettled receipts expire (the seller refunds anything it received)."""
    from apps.payments.models import BankReference, PaymentTransaction
    from apps.payments.services import apply_status

    n = 0
    for proof in open_proofs(order).select_for_update():
        proof.status, proof.rejection_reason = P.EXPIRED, reason[:255]
        proof.save(update_fields=["status", "rejection_reason", "updated_at"])
        apply_status(proof.payment_transaction_id, PaymentTransaction.Status.CANCELLED, reason=reason)
        BankReference.objects.filter(payment_id=proof.payment_transaction_id).delete()
        log_event(order, OrderEvent.Kind.NOTE, f"Receipt {proof.normalised_reference} expired: {reason}. If the "
                                               "seller received this money it must refund it.", by=by)
        n += 1
    return n


# =========================================================================== refunds (by the seller)
@transaction.atomic
def record_partner_refund(order: Order, user, *, amount, reference: str, refunded_on, evidence=None,
                          note: str = "") -> PartnerRefund:
    """The seller refunded the buyer from its own account; Nebret records and tracks it."""
    from . import operations

    member = staff_member(user, order)
    if not ((member and member.can_manage) or getattr(user, "is_staff", False)):
        raise BusinessRuleError("Only the company owner, a manager or Nebret can record a refund.")
    if not (reference or "").strip():
        raise BusinessRuleError("Enter the refund transfer reference.")
    if isinstance(refunded_on, str):
        from django.utils.dateparse import parse_date

        refunded_on = parse_date(refunded_on)
    if not refunded_on:
        raise BusinessRuleError("Enter the refund date.")
    key = {}
    if evidence is not None:
        from apps.core import private_files

        data, ctype = private_files.validate(evidence)
        key = private_files.store(data, ctype, folder=f"refunds/{order.reference}", name=getattr(evidence, "name", ""))
    elif not getattr(user, "is_staff", False):
        raise BusinessRuleError("Attach the refund receipt.")
    operations.refund_order(order, user, amount=amount, method="Bank transfer by the seller",
                            reference=reference.strip(), note=note)
    return PartnerRefund.objects.create(order=order, amount=money(amount), reference=reference.strip()[:64],
                                        refunded_on=refunded_on, evidence=key, note=note[:255], recorded_by=user)


def escalate_overdue_refunds() -> int:
    today = timezone.localdate()
    n = 0
    for order in Order.objects.filter(refund_due_by__lt=today, refund_escalated_at__isnull=True,
                                      status__in=[Order.Status.CANCELLED, Order.Status.DEFAULTED,
                                                  Order.Status.EXPIRED]):
        if order.refunded_amount >= order.amount_paid:
            continue
        Order.objects.filter(pk=order.pk).update(refund_escalated_at=timezone.now())
        _alert("REFUND_OVERDUE", f"{order.reference}: {fmt(order.amount_paid - order.refunded_amount)} ETB refund was "
                                 f"due by {order.refund_due_by:%d %b %Y}.", order=order)
        n += 1
    return n


# =========================================================================== API rows
def proof_row(proof: BankTransferProof, request=None, *, for_staff: bool = False) -> dict:
    from apps.core import private_files

    labels = {P.SUBMITTED: "Verifying with bank", P.VERIFYING: "Verifying with bank",
              P.VERIFIED_AWAITING_PARTNER: "Waiting for seller", P.CONFIRMED: "Confirmed",
              P.REJECTED: "Rejected", P.DISPUTED: "Being checked by Nebret", P.EXPIRED: "Expired"}
    row = {
        "id": proof.pk, "order": proof.order.reference, "status": proof.status,
        "status_label": labels.get(proof.status, proof.get_status_display()),
        "bank": proof.bank, "bank_label": dict(Channel.choices).get(proof.bank, proof.bank),
        "reference": proof.normalised_reference, "amount_claimed": str(proof.amount_claimed),
        "amount_verified": str(proof.amount_verified) if proof.amount_verified is not None else None,
        "currency": proof.currency, "payer_name": proof.payer_name, "payer_account": proof.payer_account,
        "transfer_datetime": proof.transfer_datetime, "created_at": proof.created_at,
        "rejection_reason": proof.rejection_reason, "requires_manual_review": proof.requires_manual_review,
        "needs_second_confirmer": proof.needs_second_confirmer and proof.status == P.VERIFIED_AWAITING_PARTNER,
        "first_confirmer": proof.partner_confirmed_by.full_name if proof.partner_confirmed_by_id else "",
        "receipt_url": private_files.signed_url(proof.file, request),
    }
    if for_staff:
        r = proof.shegerpay_response or {}
        row.update({
            "payment_code": proof.order.payment_code, "expected_amount": str(proof.order.amount_due_now),
            "balance_due": str(proof.order.balance_due), "dispute_reason": proof.dispute_reason,
            "verified_at": proof.verified_at,
            "check": {"outcome": r.get("outcome"), "reason": r.get("reason"), "amount": r.get("amount"),
                      "payer": r.get("payer"), "receiver_account": r.get("receiver_account"),
                      "narration": r.get("narration"), "checked_at": proof.shegerpay_checked_at,
                      "attempts": proof.verify_attempts},
            "dual_control": proof.amount_claimed >= dual_control_threshold(),
            "buyer_name": proof.order.buyer_name or proof.order.user.full_name,
        })
    return row
