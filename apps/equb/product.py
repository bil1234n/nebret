"""
Product Equbs – "Win this with Equb" (replaces the old "Equb payout" way to pay).

A product Equb is a normal rotating savings group linked to one listing. Members pay the contribution (e.g. daily)
through the existing Equb payment flow; in each draw, winners receive the product: an order is created for them
automatically, funded by their payout (``Funding.EQUB_PAYOUT`` through ``apply_credit``), and handed over as usual.

Rules (defaults chosen for the [DECIDE] items):

* Every member wins once, so every member eventually receives the product.
* The partner reserves one unit per member up front (unit status ``EQUB``); the price is locked at creation
  (``unit.price_override``), so later price changes don't affect the group.
* Validation: each winner's payout (contribution × duration − service fee) must cover the locked price.
"""
from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify

from .models import DurationUnit, EqubGroup, EqubMembership, EqubPayout

log = logging.getLogger(__name__)
CENT = Decimal("0.01")


def _can_create(user, listing) -> bool:
    if getattr(user, "is_staff", False):
        return True
    from apps.agencies.models import AgencyStaff

    return bool(listing.company_id) and AgencyStaff.objects.filter(
        user=user, agency_id=listing.company_id, is_active=True,
        role__in=[AgencyStaff.Role.OWNER, AgencyStaff.Role.MANAGER]).exists()


def maths(*, price, contribution, duration_value: int, fee_percent) -> dict:
    """The numbers shown to whoever creates the group (and checked)."""
    gross = (Decimal(contribution) * duration_value).quantize(CENT)
    fee = (gross * Decimal(fee_percent) / 100).quantize(CENT)
    payout = gross - fee
    return {"contribution": str(Decimal(contribution).quantize(CENT)), "payments_per_member": duration_value,
            "paid_in_per_member": str(gross), "service_fee_percent": str(fee_percent), "service_fee": str(fee),
            "payout_per_winner": str(payout), "product_price": str(Decimal(price).quantize(CENT)),
            "covers_price": payout >= Decimal(price),
            "explanation": f"{Decimal(contribution):,.2f} × {duration_value} payments = {gross:,.2f} ETB − "
                           f"{fee_percent}% fee ({fee:,.2f}) = {payout:,.2f} ETB per winner "
                           f"{'≥' if payout >= Decimal(price) else '<'} product price {Decimal(price):,.2f} ETB"}


def rules_text(listing, members: int, group_name: str) -> str:
    return (f"{group_name} is a rotating savings group (ዕቁብ) for “{listing.title}”.\n"
            f"Every member pays the same contribution every round. In each draw the winners receive the product – "
            f"every one of the {members} members wins exactly once, so every member eventually receives it.\n"
            "Winners keep paying their contributions until the group ends.\n"
            "The product price is fixed when the group is created. If a winner's payout is more than the price, the "
            "difference is paid out to them.\n"
            "Contributions are paid with the Equb payment options in the app and held under the Equb rules.\n"
            "The seller has reserved one unit for every member; the winner's order is created and handed over "
            "automatically.")


@transaction.atomic
def create_product_equb(listing, user, *, contribution_amount, duration_value: int, duration_unit: str = "DAYS",
                        members: int, start_date: date, rounds_drawn: int | None = None, name: str = "",
                        terms: str = "", draw_type: str = EqubGroup.DrawType.AUTOMATIC) -> tuple[EqubGroup, dict]:
    from apps.market.models import Listing, ListingUnit

    if not _can_create(user, listing):
        raise BusinessRuleError("Only Nebret staff or the company owner / a manager can create a product Equb.")
    listing = Listing.objects.select_for_update(of=("self",)).get(pk=listing.pk)
    if listing.status not in (Listing.Status.ACTIVE, Listing.Status.SOLD_OUT, Listing.Status.DRAFT):
        raise BusinessRuleError("This listing is archived.")
    try:
        contribution = Decimal(str(contribution_amount)).quantize(CENT)
        duration_value, members = int(duration_value), int(members)
    except Exception as exc:  # noqa: BLE001
        raise BusinessRuleError("Enter the contribution, number of payments and members.") from exc
    if contribution <= 0 or duration_value < 1 or members < 2:
        raise BusinessRuleError("Contribution > 0, at least 1 payment and at least 2 members.")
    if duration_unit not in DurationUnit.values:
        raise BusinessRuleError("Choose days, weeks or months.")
    if start_date < timezone.localdate():
        raise BusinessRuleError("The start date can't be in the past.")
    units = list(ListingUnit.objects.select_for_update().filter(listing=listing, status=ListingUnit.Status.AVAILABLE)
                 .order_by("code")[:members])
    if len(units) < members:
        raise BusinessRuleError(f"Reserve one unit per member: {members} needed, only {len(units)} available. "
                                "Add units to the listing first.")
    price = max(u.price for u in units)
    group = EqubGroup(name=(name or f"{listing.title} Equb")[:120], contribution_amount=contribution,
                      duration_value=duration_value, duration_unit=duration_unit, max_members=members,
                      rounds_drawn=rounds_drawn or None, start_date=start_date, draw_type=draw_type,
                      listing=listing, product_price=price, organizer=user, status=EqubGroup.Status.OPEN)
    m = maths(price=price, contribution=contribution, duration_value=duration_value,
              fee_percent=group.service_fee_percent)
    if not m["covers_price"]:
        raise BusinessRuleError("Each winner's payout must cover the product price: " + m["explanation"])
    group.rules_text = (terms.strip() + "\n\n" if terms.strip() else "") + rules_text(listing, members, group.name)
    group.save()
    for u in units:
        u.status, u.equb_group, u.price_override = ListingUnit.Status.EQUB, group, u.price  # price locked
        u.save(update_fields=["status", "equb_group", "price_override"])
    listing.refresh_stock_status()
    return group, m


def reserved_unit(group: EqubGroup):
    from apps.market.models import ListingUnit

    return (ListingUnit.objects.select_for_update().filter(equb_group=group, status=ListingUnit.Status.EQUB)
            .order_by("code").first())


def on_payouts_issued(payouts: list[EqubPayout]):
    """Called by ``run_draw``: winners of a product Equb get their order straight away."""
    for payout in payouts:
        group = payout.round.group
        if not group.listing_id:
            continue
        try:
            with transaction.atomic():
                create_winner_order(payout)
        except Exception as exc:  # noqa: BLE001 – the draw stands; staff finish the order by hand
            log.exception("Product Equb order failed for payout %s", payout.pk)
            from apps.payments.models import FraudAlert

            FraudAlert.objects.create(kind=FraudAlert.Kind.EQUB_ORDER_FAILED,
                                      message=f"{group.name}: order for winner {payout.membership.display_name} "
                                              f"could not be created – {exc}"[:255],
                                      user=payout.membership.user, agency_id=group.listing.company_id,
                                      data={"payout": payout.pk, "group": group.slug})


def create_winner_order(payout: EqubPayout):
    from apps.market.models import Order
    from apps.market.services.orders import apply_credit, create_order

    payout = EqubPayout.objects.select_for_update().select_related("membership__user", "round__group").get(pk=payout.pk)
    if payout.status != EqubPayout.Status.ISSUED:
        return payout.order
    group = payout.round.group
    unit = reserved_unit(group)
    if unit is None:
        raise BusinessRuleError("No reserved unit left for this winner.")
    m = payout.membership
    order = create_order(user=m.user, listing=group.listing, plan=Order.Plan.EQUB_PAYOUT, unit_id=unit.pk,
                         buyer_name=m.dependent_name or m.user.full_name, product_equb=group,
                         note=f"Won in {group.name}, round {payout.round.number}")
    order = apply_credit(order, payout.amount, Order.Funding.EQUB_PAYOUT, payout.voucher_code)
    payout.status, payout.order, payout.redeemed_at = EqubPayout.Status.REDEEMED, order, timezone.now()
    payout.save(update_fields=["status", "order", "redeemed_at", "updated_at"])
    who = m.dependent_name or "You"
    transaction.on_commit(lambda: notify(
        m.user, Notification.Kind.EQUB_WIN, f"🎉 {who} won {group.listing.title}!",
        f"Order {order.reference} was created and paid with your Equb payout. The seller will arrange the handover – "
        "your handover pass is in the order.", {"order": order.reference, "equb": group.slug}))
    return order


def release_spare_units() -> int:
    """Scheduler: units reserved for groups that were cancelled, or beyond the members still waiting to win."""
    from apps.market.models import ListingUnit

    n = 0
    groups = EqubGroup.objects.filter(listing__isnull=False, reserved_units__status=ListingUnit.Status.EQUB).distinct()
    for g in groups:
        reserved = list(ListingUnit.objects.filter(equb_group=g, status=ListingUnit.Status.EQUB).order_by("-code"))
        if g.status == EqubGroup.Status.CANCELLED:
            keep = 0
        elif g.status in (EqubGroup.Status.DRAFT, EqubGroup.Status.OPEN):
            keep = len(reserved)
        else:
            keep = g.memberships.filter(status=EqubMembership.Status.ACTIVE, has_won=False).count()
        for u in reserved[:max(len(reserved) - keep, 0)]:
            ListingUnit.objects.filter(pk=u.pk).update(status=ListingUnit.Status.AVAILABLE, equb_group=None,
                                                       price_override=None)
            n += 1
        if reserved and keep < len(reserved):
            g.listing.refresh_stock_status()
    return n


def group_row(g: EqubGroup) -> dict:
    return {"slug": g.slug, "name": g.name, "status": g.status, "contribution_amount": str(g.contribution_amount),
            "every": g.every, "members": g.max_members, "joined": g.members_count, "spots_left": g.spots_left,
            "start_date": g.start_date, "product_price": str(g.product_price or ""),
            "payout_per_winner": str(g.payout_per_winner), "reserved_units": g.reserved_units.filter(status="EQUB").count()}
