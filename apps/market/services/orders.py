"""
Order lifecycle.

create (unit held) → first payment → RESERVED | INSTALLMENTS | PAID → handover → COMPLETED
                   ↘ expire (not paid in time) / cancel

Every payment – app, office, Equb payout credit – goes through :func:`apply_payment`, so the rules are the same
everywhere: the first payment confirms the order, later money settles the reservation balance or the oldest unpaid
installments, and the order is PAID once nothing is left.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.equb.models import add_months
from apps.notifications.models import Notification
from apps.notifications.services import notify

from ..models import Installment, Listing, ListingUnit, Order, OrderEvent, money
from . import pricing

log = logging.getLogger(__name__)
ZERO = Decimal("0")


def fmt(v) -> str:
    v = Decimal(v or 0)
    return f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}"


def _hold_window() -> timedelta:
    return timedelta(minutes=settings.NEBRET["RESERVATION_HOLD_MINUTES"])


def log_event(order: Order, kind: str, message: str, by=None) -> OrderEvent:
    return OrderEvent.objects.create(order=order, kind=kind, message=message[:255], by=by)


# =========================================================================== stock
def _receipts_in_review():
    """
    Order ids with a bank receipt still being checked (one PENDING ``bank`` PaymentTransaction per uploaded receipt).
    Such an order may already be paid – the money just isn't confirmed yet – so it is never expired automatically.
    """
    from django.contrib.contenttypes.models import ContentType

    from apps.payments.models import PaymentTransaction

    return PaymentTransaction.objects.filter(
        content_type=ContentType.objects.get_for_model(Order), provider=PaymentTransaction.Provider.BANK,
        status__in=[PaymentTransaction.Status.INITIATED, PaymentTransaction.Status.PENDING],
    ).values("object_id")


def protecting_orders(now=None):
    """
    Unpaid orders that still have a claim on their unit: the hold hasn't run out, money has been paid, or a receipt
    is under review. A unit is never put back on sale automatically while one of these exists – only staff (cancel)
    can release it.
    """
    now = now or timezone.now()
    return Order.objects.filter(status=Order.Status.PENDING_PAYMENT).filter(
        Q(expires_at__gt=now) | Q(expires_at__isnull=True) | Q(amount_paid__gt=0) | Q(pk__in=_receipts_in_review()))


def _unit_is_protected(unit: ListingUnit, now=None) -> bool:
    return protecting_orders(now).filter(unit=unit).exists()


def release_stale_holds() -> int:
    """Units held by a checkout that was abandoned (nothing paid, nothing under review) go back on sale."""
    now = timezone.now()
    stale = ListingUnit.objects.filter(status=ListingUnit.Status.HELD, held_until__lt=now)
    listing_ids = set(stale.values_list("listing_id", flat=True))
    n = stale.exclude(Exists(protecting_orders(now).filter(unit=OuterRef("pk")))).update(
        status=ListingUnit.Status.AVAILABLE, held_by=None, held_until=None)
    for listing in Listing.objects.filter(id__in=listing_ids):
        listing.refresh_stock_status()
    return n


def _pick_unit(listing: Listing, unit_id=None) -> ListingUnit:
    qs = ListingUnit.objects.select_for_update().filter(listing=listing)
    if unit_id:
        unit = qs.filter(pk=unit_id).first()
        if not unit:
            raise BusinessRuleError("That unit is not part of this listing.")
    else:
        unit = qs.filter(status=ListingUnit.Status.AVAILABLE).order_by("code").first()
        if not unit:
            raise BusinessRuleError("Sorry, this item is sold out.")
    free = unit.status == ListingUnit.Status.AVAILABLE or (
        unit.status == ListingUnit.Status.HELD and unit.held_until and unit.held_until <= timezone.now()
        and not _unit_is_protected(unit))
    if not free:
        raise BusinessRuleError(f"{unit.label or unit.code} has just been reserved by someone else. Choose another one.")
    return unit


def _release_unit(order: Order):
    if not order.unit_id:
        return
    ListingUnit.objects.filter(pk=order.unit_id).update(status=ListingUnit.Status.AVAILABLE, held_by=None,
                                                        held_until=None)
    order.listing.refresh_stock_status()


# =========================================================================== create
@transaction.atomic
def create_order(*, user, listing: Listing, plan: str, unit_id=None, months: int = 0, down_payment=None,
                 delivery_method: str = Order.Delivery.PICKUP, delivery_address: str = "", buyer_name: str = "",
                 buyer_id_number: str = "", contact_phone: str = "", contact_email: str = "", note: str = "",
                 funding_source: str = Order.Funding.DIRECT, by=None, product_equb=None) -> Order:
    """
    Every new order is paid by bank transfer straight into the seller's account: it gets a payment code and a frozen
    copy of that account, and the unit is held for ``FIRST_RECEIPT_HOLD_HOURS`` for the first receipt.
    ``product_equb`` (internal): the order for a product-Equb winner, paid by the Equb payout.
    """
    # of=("self",): PostgreSQL can't lock the nullable side of the outer join to the company.
    listing = Listing.objects.select_for_update(of=("self",)).select_related("company", "category").get(pk=listing.pk)
    if product_equb is None:
        if plan == Order.Plan.EQUB_PAYOUT:
            raise BusinessRuleError("“Equb payout” is no longer a way to pay. Join this product's Equb instead, "
                                    "or choose another way to pay.")
        if listing.status not in (Listing.Status.ACTIVE, Listing.Status.SOLD_OUT):
            raise BusinessRuleError("This item is not for sale right now.")
    if listing.company_id and listing.company.status == "SUSPENDED":
        raise BusinessRuleError("This seller is not taking orders right now.")
    delivery = delivery_method == Order.Delivery.DELIVERY
    if delivery and len(delivery_address.strip()) < 5:
        raise BusinessRuleError("Enter the delivery address.")
    if product_equb is not None:
        unit = ListingUnit.objects.select_for_update().get(pk=unit_id, equb_group=product_equb)
        q = pricing.quote(listing, unit=unit, plan=Order.Plan.FULL, delivery=delivery, any_plan=True)
        plan, funding_source = Order.Plan.EQUB_PAYOUT, Order.Funding.EQUB_PAYOUT
    else:
        unit = _pick_unit(listing, unit_id)
        q = pricing.quote(listing, unit=unit, plan=plan, months=months, down_payment=down_payment, delivery=delivery)
    now = timezone.now()
    order = Order(
        user=user, listing=listing, unit=unit, company=listing.company, plan=plan,
        unit_price=q.unit_price, delivery_fee=q.delivery_fee, finance_charge=q.finance_charge,
        total_amount=q.total, first_payment=q.first_payment, installment_months=q.months,
        currency=listing.currency, price_breakdown=q.as_dict(), funding_source=funding_source,
        delivery_method=delivery_method, delivery_address=delivery_address.strip()[:255],
        buyer_name=(buyer_name or user.full_name or "").strip()[:120], buyer_id_number=buyer_id_number.strip()[:40],
        contact_phone=(contact_phone or user.phone)[:16], contact_email=(contact_email or user.email or "")[:254],
        note=note.strip()[:255], expires_at=now + timedelta(hours=settings.NEBRET["FIRST_RECEIPT_HOLD_HOURS"]),
    )
    if product_equb is not None:
        order.payment_method, order.expires_at = Order.PaymentMethod.EQUB, None
        order.save()
        unit.status, unit.held_by, unit.held_until = ListingUnit.Status.RESERVED, user, None
        unit.save(update_fields=["status", "held_by", "held_until"])
        log_event(order, OrderEvent.Kind.CREATED, f"Won in {product_equb.name} · {unit.label or unit.code}.", by=by)
        return order
    from .bank_transfer import pay_to_account
    from ..models import new_payment_code

    order.pay_to_account = pay_to_account(order)
    order.payment_code = new_payment_code()
    order.payment_method = Order.PaymentMethod.BANK_TRANSFER
    order.save()
    unit.status, unit.held_by, unit.held_until = ListingUnit.Status.HELD, user, order.expires_at
    unit.save(update_fields=["status", "held_by", "held_until"])
    listing.refresh_stock_status()
    log_event(order, OrderEvent.Kind.CREATED,
              f"{order.get_plan_display()} · {unit.label or unit.code} held until "
              f"{timezone.localtime(order.expires_at):%d %b %H:%M} for the first bank-transfer receipt "
              f"(payment code {order.payment_code}).", by=by or user)
    from apps.chat.services import link_order

    transaction.on_commit(lambda: link_order(order.pk))
    return order


# =========================================================================== payments
def _schedule_installments(order: Order, start=None):
    if order.installments.exists() or order.installment_months <= 0:
        return
    start = start or timezone.localdate()
    financed = order.total_amount - order.first_payment
    regular, last = pricing.installment_split(financed, order.installment_months)
    Installment.objects.bulk_create([
        Installment(order=order, number=n, due_date=add_months(start, n),
                    amount=last if n == order.installment_months else regular)
        for n in range(1, order.installment_months + 1)
    ])


def _settle_installments(order: Order, amount: Decimal) -> tuple[Decimal, list[int]]:
    """Pay the oldest unpaid installments first (late fees included). Returns (unused, numbers fully paid)."""
    left, paid = Decimal(amount), []
    for inst in order.installments.select_for_update().exclude(
            status__in=[Installment.Status.PAID, Installment.Status.WAIVED]).order_by("number"):
        if left <= 0:
            break
        take = min(left, inst.amount_due)
        inst.paid_amount += take
        left -= take
        if inst.amount_due <= 0:
            inst.status, inst.paid_at = Installment.Status.PAID, timezone.now()
            paid.append(inst.number)
        inst.save(update_fields=["paid_amount", "status", "paid_at"])
    return left, paid


def _paid_in_full(order: Order):
    order.status = Order.Status.PAID
    order.paid_in_full_at = timezone.now()
    order.handover_due = timezone.localdate() + timedelta(days=order.listing.handover_days)
    order.expires_at = None
    if order.unit_id:
        ListingUnit.objects.filter(pk=order.unit_id).update(status=ListingUnit.Status.SOLD, held_until=None)
    log_event(order, OrderEvent.Kind.PAID_IN_FULL, f"Paid in full – handover by {order.handover_due:%d %b %Y}.")


@transaction.atomic
def apply_payment(order: Order, amount, *, reference: str = "", by=None, label: str = "Payment") -> Order:
    """Money received for an order (idempotency is the caller's job – payments run fulfilment once)."""
    order = Order.objects.select_for_update().select_related("listing", "user").get(pk=order.pk)
    amount = money(amount)
    if amount <= 0:
        return order
    if order.status not in (Order.Status.PENDING_PAYMENT, Order.Status.RESERVED, Order.Status.INSTALLMENTS):
        # Paid after it expired or was cancelled: keep the money traceable for a refund.
        log.error("Payment of %s received for %s in status %s", amount, order.reference, order.status)
        log_event(order, OrderEvent.Kind.PAYMENT, f"{label} of {fmt(amount)} ETB received while the order was "
                                                  f"{order.get_status_display().lower()} – refund it.", by=by)
        order.amount_paid += amount
        order.save(update_fields=["amount_paid", "updated_at"])
        return order

    applied = min(amount, order.balance_due)
    order.amount_paid += applied
    log_event(order, OrderEvent.Kind.PAYMENT, f"{label}: {fmt(applied)} ETB" + (f" (ref {reference})" if reference else "")
              + (f" – {fmt(amount - applied)} ETB more than owed, to be refunded." if amount > applied else "."), by=by)
    was_pending = order.status == Order.Status.PENDING_PAYMENT

    if was_pending and order.amount_paid >= order.first_payment:
        order.confirmed_at = timezone.now()
        order.expires_at = None
        if order.unit_id:
            ListingUnit.objects.filter(pk=order.unit_id).update(status=ListingUnit.Status.RESERVED, held_until=None)
        if order.plan == Order.Plan.RESERVATION and order.balance_due > 0:
            order.status = Order.Status.RESERVED
            order.reserved_until = timezone.localdate() + timedelta(days=order.listing.reservation_days)
            log_event(order, OrderEvent.Kind.CONFIRMED,
                      f"Reserved – pay the balance of {fmt(order.balance_due)} ETB by {order.reserved_until:%d %b %Y}.")
        elif order.plan == Order.Plan.INSTALLMENT and order.balance_due > 0:
            order.status = Order.Status.INSTALLMENTS
            order.save()
            _schedule_installments(order)
            extra = order.amount_paid - order.first_payment
            if extra > 0:
                _settle_installments(order, extra)
            log_event(order, OrderEvent.Kind.CONFIRMED,
                      f"Down payment received – {order.installment_months} monthly installments scheduled.")
    elif order.status == Order.Status.INSTALLMENTS:
        _, numbers = _settle_installments(order, applied)
        if numbers:
            log_event(order, OrderEvent.Kind.INSTALLMENT,
                      "Installment " + ", ".join(f"#{n}" for n in numbers) + " paid.")

    if order.balance_due <= 0 and order.status != Order.Status.PAID:
        _paid_in_full(order)
    order.save()
    _notify_payment(order, applied, was_pending)
    return order


def _notify_payment(order: Order, amount: Decimal, was_pending: bool):
    if order.status == Order.Status.PAID:
        title, body = "Paid in full 🎉", (f"{order.listing.title} is fully paid ({order.reference}). Your handover "
                                          f"pass is ready – handover by {order.handover_due:%d %b %Y}.")
    elif was_pending and order.status == Order.Status.RESERVED:
        title, body = "Reserved ✅", (f"{order.title} is reserved for you. Pay the balance of {fmt(order.balance_due)} "
                                     f"ETB by {order.reserved_until:%d %b %Y}.")
    elif was_pending and order.status == Order.Status.INSTALLMENTS:
        nxt = order.next_installment
        title, body = "Order confirmed ✅", (f"Down payment received for {order.listing.title}. First installment of "
                                            f"{fmt(nxt.amount if nxt else 0)} ETB is due "
                                            f"{nxt.due_date:%d %b %Y}." if nxt else "")
    else:
        title, body = "Payment received ✅", (f"{fmt(amount)} ETB received for {order.reference}. "
                                             f"Balance left: {fmt(order.balance_due)} ETB.")
    transaction.on_commit(lambda: notify(order.user, Notification.Kind.ORDER, title, body,
                                         {"order": order.reference}))


def apply_credit(order: Order, credit, source: str, reference: str) -> Order:
    """Equb payout / giveaway prize used on an order. Anything above the balance is paid out to the member."""
    order = Order.objects.get(pk=order.pk)
    credit = money(credit)
    usable = min(credit, order.balance_due)
    remainder = credit - usable
    Order.objects.filter(pk=order.pk).update(
        funding_source=source, funding_reference=reference[:64],
        price_breakdown={**(order.price_breakdown or {}), "credit_applied": str(usable),
                         "credit_remainder": str(remainder)})
    order = apply_payment(order, usable, reference=reference, label="Equb payout" if source == Order.Funding.EQUB_PAYOUT
                          else "Prize credit")
    if remainder > 0:
        log_event(order, OrderEvent.Kind.NOTE,
                  f"{fmt(remainder)} ETB of {reference} was more than the order needed – pay it out to the member.")
    return order


# --- payments app integration ---------------------------------------------------------------------
def _order_of(target) -> Order | None:
    if isinstance(target, Order):
        return target
    if isinstance(target, Installment):
        return target.order
    return None


def on_order_paid(txn):
    order = _order_of(txn.target)
    if order:
        label = "Installment" if txn.purpose == "INSTALLMENT" else (
            "Reservation fee" if order.plan == Order.Plan.RESERVATION and order.status == Order.Status.PENDING_PAYMENT
            else "Payment")
        apply_payment(order, txn.amount, reference=str(txn.provider_reference or txn.reference),
                      by=getattr(txn, "recorded_by", None), label=f"{label} via {txn.get_provider_display()}")


def on_order_payment_failed(txn):
    if txn.provider == "bank":
        return  # receipts: the receipt service already logged the rejection and told the buyer why
    order = _order_of(txn.target)
    if order:
        log_event(order, OrderEvent.Kind.PAYMENT,
                  f"{txn.get_provider_display()} payment of {fmt(txn.amount)} ETB failed"
                  + (f": {txn.failure_reason}" if txn.failure_reason else "."))
    notify(txn.user, Notification.Kind.PAYMENT, "Payment not completed",
           "Your payment didn't go through. Nothing was charged – you can try again from the order.",
           {"payment": str(txn.reference), **({"order": order.reference} if order else {})})


def installment_options(order: Order) -> list[dict]:
    """Pay 1…N installments at once (oldest first)."""
    out, running = [], ZERO
    for i, inst in enumerate(order.installments.exclude(
            status__in=[Installment.Status.PAID, Installment.Status.WAIVED]).order_by("number"), start=1):
        running += inst.amount_due
        out.append({"count": i, "to_number": inst.number, "due_date": inst.due_date.isoformat(),
                    "amount": str(money(running))})
    return out


def start_payment(order: Order, user, *, count: int = 1, amount=None, provider=None, payer_phone=""):
    """App checkout for whatever is due now: first payment, reservation balance or 1…N installments."""
    from apps.payments.models import PaymentTransaction
    from apps.payments.services import initiate_payment

    if order.user_id != user.id:
        raise BusinessRuleError("You can only pay for your own order.")
    if order.payment_method != Order.PaymentMethod.LEGACY:
        raise BusinessRuleError("Marketplace payments are made by bank transfer straight to the seller. Open the "
                                "order to see the account and payment code, then upload your receipt.")
    if order.status == Order.Status.PENDING_PAYMENT:
        if not order.is_payable:
            raise BusinessRuleError("This order has expired. Place a new order.")
        due, purpose = order.amount_due_now, PaymentTransaction.Purpose.ORDER
    elif order.status == Order.Status.RESERVED:
        due, purpose = order.balance_due, PaymentTransaction.Purpose.ORDER
        if amount not in (None, ""):
            due = min(max(money(amount), Decimal("1")), order.balance_due)
    elif order.status == Order.Status.INSTALLMENTS:
        options = installment_options(order)
        if not options:
            raise BusinessRuleError("Nothing is due on this order.")
        count = max(1, min(int(count or 1), len(options)))
        due, purpose = Decimal(options[count - 1]["amount"]), PaymentTransaction.Purpose.INSTALLMENT
    else:
        raise BusinessRuleError("Nothing is due on this order.")
    if due <= 0:
        raise BusinessRuleError("Nothing is due on this order.")
    return initiate_payment(user=user, target=order, amount=due, purpose=purpose, provider=provider,
                            payer_phone=payer_phone or order.contact_phone)


# =========================================================================== cancel / expire / defaults
def cancellation_policy(order: Order) -> dict:
    out = {"can_cancel": False, "refundable": False, "refund_estimate": "0.00", "message": "", "code": ""}
    in_review = order.proofs.filter(status__in=["SUBMITTED", "VERIFYING", "VERIFIED_AWAITING_PARTNER",
                                                "DISPUTED"]).exists()
    if order.status == Order.Status.PENDING_PAYMENT and order.amount_paid <= 0 and not in_review:
        out.update(can_cancel=True, code="unpaid", message="Nothing has been paid yet. Cancelling releases the item.")
    elif order.status == Order.Status.PENDING_PAYMENT and in_review:
        out.update(code="in_review", message="A receipt is being checked. Chat with Nebret support if you want to "
                                             "cancel – the seller refunds anything it received and Nebret tracks it.")
    elif order.status == Order.Status.RESERVED:
        out.update(code="reserved", message="Reservation fees are non-refundable once the item is reserved. "
                                            "Chat with the seller or Nebret support to change your order.")
    elif order.status in (Order.Status.INSTALLMENTS, Order.Status.PAID):
        out.update(code="paid", message="This order is paid or being paid. Chat with Nebret support to cancel – the "
                                        "seller refunds you under its terms and Nebret tracks the refund.")
    else:
        out.update(code="closed", message="This order can no longer be changed.")
    return out


@transaction.atomic
def cancel_order(order: Order, reason: str = "Cancelled by customer", *, staff=None, notify_customer=True) -> Order:
    order = Order.objects.select_for_update().select_related("listing", "user").get(pk=order.pk)
    if order.status not in Order.OPEN_STATUSES:
        raise BusinessRuleError("This order cannot be cancelled.")
    if staff is None and not cancellation_policy(order)["can_cancel"]:
        raise BusinessRuleError(cancellation_policy(order)["message"])
    was_paid = order.amount_paid > 0
    order.status = Order.Status.CANCELLED
    order.cancelled_at = timezone.now()
    order.cancellation_reason = (reason or "Cancelled")[:255]
    order.cancelled_by = staff
    order.expires_at = None
    if was_paid and order.payment_method == Order.PaymentMethod.BANK_TRANSFER:
        order.refund_due_by = timezone.localdate() + timedelta(days=settings.NEBRET["REFUND_DEADLINE_DAYS"])
    order.save()
    from .bank_transfer import expire_open_proofs

    expire_open_proofs(order, "Order cancelled", by=staff)
    _release_unit(order)
    order.installments.exclude(status=Installment.Status.PAID).update(status=Installment.Status.WAIVED)
    log_event(order, OrderEvent.Kind.CANCELLED, reason or "Cancelled", by=staff or order.user)
    if notify_customer and (staff is not None or was_paid):
        body = f"Order {order.reference} ({order.listing.title}) has been cancelled."
        if was_paid and order.refund_due_by:
            body += (f" The seller refunds you by {order.refund_due_by:%d %b %Y}; Nebret tracks the refund and "
                     "confirms it in the app.")
        elif was_paid:
            body += " Our team will process any refund due to you and confirm it in the app."
        transaction.on_commit(lambda: notify(order.user, Notification.Kind.ORDER, "Order cancelled", body,
                                             {"order": order.reference}))
    return order


def expire_unpaid_orders() -> int:
    """Orders not paid in time expire and free their unit – never one with money paid or a receipt under review."""
    n = 0
    for order in Order.objects.filter(status=Order.Status.PENDING_PAYMENT, expires_at__lt=timezone.now(),
                                      amount_paid__lte=0).exclude(pk__in=_receipts_in_review()):
        with transaction.atomic():
            o = Order.objects.select_for_update().select_related("listing").get(pk=order.pk)
            if (o.status != Order.Status.PENDING_PAYMENT or o.amount_paid > 0
                    or _receipts_in_review().filter(object_id=o.pk).exists()):
                continue
            o.status = Order.Status.EXPIRED
            o.save(update_fields=["status", "updated_at"])
            _release_unit(o)
            log_event(o, OrderEvent.Kind.EXPIRED, "Not paid in time – the item is back on sale.")
            n += 1
    release_stale_holds()
    return n


def sweep_installments() -> dict:
    """Flag overdue installments (no penalty is charged), overdue notices, and reminders a few days before the due date."""
    today = timezone.localdate()
    grace = settings.NEBRET["INSTALLMENT_GRACE_DAYS"]
    fee_pct = Decimal(str(settings.NEBRET["INSTALLMENT_LATE_FEE_PERCENT"]))
    late = reminded = 0
    open_qs = Installment.objects.filter(order__status=Order.Status.INSTALLMENTS).select_related("order__user",
                                                                                                    "order__listing")
    for inst in open_qs.filter(status=Installment.Status.PENDING, due_date__lt=today - timedelta(days=grace)):
        inst.status = Installment.Status.LATE
        inst.save(update_fields=["status"])  # no late penalty is charged
        log_event(inst.order, OrderEvent.Kind.INSTALLMENT,
                  f"Installment #{inst.number} is late.")
        notify(inst.order.user, Notification.Kind.INSTALLMENT, "Installment overdue",
               f"Installment #{inst.number} for {inst.order.listing.title} ({fmt(inst.amount_due)} ETB) is overdue. "
               "Pay now to keep your order in good standing.", {"order": inst.order.reference})
        late += 1
    soon = today + timedelta(days=3)
    for inst in open_qs.filter(status=Installment.Status.PENDING, due_date__lte=soon, due_date__gte=today,
                               reminder_sent_at__isnull=True):
        notify(inst.order.user, Notification.Kind.INSTALLMENT, "Installment due soon",
               f"Installment #{inst.number} of {fmt(inst.amount_due)} ETB for {inst.order.listing.title} is due "
               f"{inst.due_date:%d %b}.", {"order": inst.order.reference})
        Installment.objects.filter(pk=inst.pk).update(reminder_sent_at=timezone.now())
        reminded += 1
    lapsed = 0
    for order in Order.objects.filter(status=Order.Status.RESERVED, reserved_until__lt=today):
        log_event(order, OrderEvent.Kind.NOTE, f"Reservation ended on {order.reserved_until:%d %b %Y} with "
                                               f"{fmt(order.balance_due)} ETB still unpaid – follow up.")
        Order.objects.filter(pk=order.pk).update(status=Order.Status.DEFAULTED)
        notify(order.user, Notification.Kind.ORDER, "Reservation ended",
               f"Your reservation of {order.listing.title} ended before the balance was paid. Contact Nebret Equb.",
               {"order": order.reference})
        lapsed += 1
    return {"late": late, "reminded": reminded, "reservations_lapsed": lapsed}


# =========================================================================== handover
@transaction.atomic
def complete_handover(order: Order, *, staff, note: str = "") -> Order:
    order = Order.objects.select_for_update().select_related("listing", "user").get(pk=order.pk)
    if order.status != Order.Status.PAID:
        raise BusinessRuleError("Only fully paid orders can be handed over.")
    order.status = Order.Status.COMPLETED
    order.handed_over_at = timezone.now()
    order.handed_over_by = staff
    order.handover_note = note[:255]
    order.save()
    log_event(order, OrderEvent.Kind.HANDED_OVER, note or "Handed over to the customer.", by=staff)
    transaction.on_commit(lambda: notify(
        order.user, Notification.Kind.ORDER, "Handed over – congratulations! 🔑",
        f"{order.listing.title} is now yours. Thank you for buying with Nebret Equb.", {"order": order.reference}))
    return order
