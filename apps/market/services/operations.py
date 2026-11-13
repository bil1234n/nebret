"""Staff / company back-office actions on orders: office payments, refunds, notes, messages, installment waivers."""
from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify
from apps.payments.models import PaymentTransaction

from ..models import Installment, Order, OrderEvent, money
from .orders import apply_payment, fmt, log_event

OFFICE_METHODS = {"cash": "Cash", "bank": "Bank transfer", "mobile": "Mobile money"}


@transaction.atomic
def record_office_payment(order: Order, staff, *, amount, method: str, reference: str = "", note: str = "") -> Order:
    """Cash / bank / mobile money taken at the office or at the seller's showroom."""
    if method not in OFFICE_METHODS:
        raise BusinessRuleError("Choose cash, bank transfer or mobile money.")
    if order.status not in (Order.Status.PENDING_PAYMENT, Order.Status.RESERVED, Order.Status.INSTALLMENTS):
        raise BusinessRuleError("Nothing is due on this order.")
    amount = money(amount)
    if amount <= 0:
        raise BusinessRuleError("Enter the amount received.")
    if amount > order.balance_due:
        raise BusinessRuleError(f"Only {fmt(order.balance_due)} ETB is left to pay on this order.")
    if method in ("bank", "mobile") and not reference.strip():
        raise BusinessRuleError("Enter the transfer reference so the payment can be traced.")
    purpose = (PaymentTransaction.Purpose.INSTALLMENT if order.status == Order.Status.INSTALLMENTS
               else PaymentTransaction.Purpose.ORDER)
    txn = PaymentTransaction.objects.create(
        user=order.user, provider=method, purpose=purpose, amount=amount, status=PaymentTransaction.Status.SUCCESS,
        target=order, provider_reference=reference.strip()[:128] or order.reference, payer_phone=order.contact_phone,
        completed_at=timezone.now(), fulfilled=True, recorded_by=staff, agency=order.company,
        platform_fee=order.company.commission_on(amount) if order.company_id else Decimal("0"),
        raw_request={"recorded_by": getattr(staff, "full_name", ""), "note": note[:255]},
    )
    order = apply_payment(order, amount, reference=reference.strip(), by=staff,
                          label=f"{OFFICE_METHODS[method]} at the office")
    if note:
        log_event(order, OrderEvent.Kind.NOTE, note, by=staff)
    if order.company_id:
        from apps.agencies.services import record_commission

        record_commission(txn)
    return order


@transaction.atomic
def refund_order(order: Order, staff, *, amount, method: str, reference: str = "", note: str = "") -> Order:
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.status not in (Order.Status.CANCELLED, Order.Status.DEFAULTED, Order.Status.EXPIRED):
        raise BusinessRuleError("Cancel the order before recording a refund.")
    amount = money(amount)
    left = order.amount_paid - order.refunded_amount
    if amount <= 0 or amount > left:
        raise BusinessRuleError(f"Refund between 0.01 and {fmt(left)} ETB.")
    order.refunded_amount += amount
    order.refunded_at = timezone.now()
    order.refund_method = method[:20]
    order.refund_reference = reference[:64]
    if order.refunded_amount >= order.amount_paid:
        order.status = Order.Status.REFUNDED
    order.save()
    log_event(order, OrderEvent.Kind.REFUNDED, f"Refunded {fmt(amount)} ETB by {method}"
              + (f" (ref {reference})" if reference else "") + (f" – {note}" if note else "."), by=staff)
    if order.company_id:
        from apps.agencies.services import reverse_for_refund

        reverse_for_refund(order)
    transaction.on_commit(lambda: notify(order.user, Notification.Kind.ORDER, "Refund sent",
                                         f"{fmt(amount)} ETB for order {order.reference} was refunded by {method}.",
                                         {"order": order.reference}))
    return order


def waive_installment_fee(inst: Installment, staff, reason: str = "") -> Installment:
    if inst.late_fee <= 0:
        raise BusinessRuleError("There is no late fee on this installment.")
    old = inst.late_fee
    inst.late_fee = Decimal("0")
    if inst.status == Installment.Status.LATE and inst.amount_due > 0:
        inst.status = Installment.Status.PENDING
    inst.save(update_fields=["late_fee", "status"])
    log_event(inst.order, OrderEvent.Kind.NOTE, f"Late fee of {fmt(old)} ETB on installment #{inst.number} waived"
              + (f": {reason}" if reason else "."), by=staff)
    return inst


def add_note(order: Order, staff, text: str) -> OrderEvent:
    if not text.strip():
        raise BusinessRuleError("Write a note first.")
    return log_event(order, OrderEvent.Kind.NOTE, text.strip(), by=staff)


def message_customer(order: Order, staff, title: str, body: str):
    if not body.strip():
        raise BusinessRuleError("Write the message first.")
    notify(order.user, Notification.Kind.ORDER, (title or "Message about your order")[:120], body.strip()[:500],
           {"order": order.reference})
    log_event(order, OrderEvent.Kind.MESSAGE, f"Message sent: {body.strip()}", by=staff)
