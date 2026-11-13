"""
Price quotes for every payment plan.

The quote is always computed on the server (the app only shows it), and the same function builds the order,
so what the customer saw is exactly what they are charged.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from django.conf import settings

from apps.core.exceptions import BusinessRuleError

from ..models import Listing, ListingUnit, Order, money

ZERO = Decimal("0")


@dataclass
class Quote:
    plan: str
    unit_price: Decimal
    delivery_fee: Decimal = ZERO
    finance_charge: Decimal = ZERO
    total: Decimal = ZERO
    first_payment: Decimal = ZERO
    months: int = 0
    monthly: Decimal = ZERO
    last_month: Decimal = ZERO
    reservation_days: int = 0
    lines: list = field(default_factory=list)

    @property
    def financed(self) -> Decimal:
        return max(self.total - self.first_payment, ZERO)

    def as_dict(self) -> dict:
        out = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}
        out["financed"] = str(self.financed)
        return out


def installment_split(amount: Decimal, months: int) -> tuple[Decimal, Decimal]:
    """(regular installment, last installment) – the last one absorbs the rounding."""
    if months <= 0:
        return ZERO, ZERO
    regular = money(amount / months)
    last = money(amount - regular * (months - 1))
    return regular, last


def quote(listing: Listing, *, unit: ListingUnit | None = None, plan: str = Order.Plan.FULL, months: int = 0,
          down_payment=None, delivery: bool = False) -> Quote:
    if plan not in listing.payment_plans():
        raise BusinessRuleError("The seller does not offer this way to pay for this item.")
    price = money(unit.price if unit else listing.price)
    delivery_fee = money(listing.delivery_fee) if delivery and listing.delivery_available else ZERO
    if delivery and not listing.delivery_available:
        raise BusinessRuleError("Delivery is not available for this item – choose handover at the seller.")
    q = Quote(plan=plan, unit_price=price, delivery_fee=delivery_fee)
    q.lines.append({"label": "Price", "amount": str(price)})
    if delivery_fee:
        q.lines.append({"label": "Delivery", "amount": str(delivery_fee)})
    base = price + delivery_fee

    if plan in (Order.Plan.FULL, Order.Plan.EQUB_PAYOUT):
        q.total = q.first_payment = base

    elif plan == Order.Plan.RESERVATION:
        q.total = base
        q.first_payment = min(money(listing.reservation_fee), base)
        q.reservation_days = listing.reservation_days
        q.lines.append({"label": f"Reservation fee (pay the balance within {listing.reservation_days} days)",
                        "amount": str(q.first_payment)})

    elif plan == Order.Plan.INSTALLMENT:
        months = int(months or listing.max_installment_months)
        if not 1 <= months <= min(listing.max_installment_months, settings.NEBRET["MAX_INSTALLMENT_MONTHS"]):
            raise BusinessRuleError(f"Choose between 1 and {listing.max_installment_months} months.")
        minimum = money(price * listing.min_down_payment_percent / 100)
        down = money(down_payment if down_payment not in (None, "") else minimum)
        if down < minimum:
            raise BusinessRuleError(f"The down payment must be at least {minimum:,.2f} ETB "
                                    f"({listing.min_down_payment_percent:g}% of the price).")
        if down >= base:
            raise BusinessRuleError("The down payment covers the whole price – choose full payment instead.")
        financed = base - down
        charge = money(financed * listing.finance_rate_percent / 100 * months / 12)
        q.finance_charge = charge
        q.total = base + charge
        q.first_payment = down
        q.months = months
        q.monthly, q.last_month = installment_split(financed + charge, months)
        q.lines.append({"label": "Down payment", "amount": str(down)})
        if charge:
            q.lines.append({"label": f"Finance charge ({listing.finance_rate_percent:g}% a year, flat)",
                            "amount": str(charge)})
        q.lines.append({"label": f"{months} monthly installments", "amount": str(q.monthly)})
    else:  # pragma: no cover
        raise BusinessRuleError("Unknown payment plan.")

    q.lines.append({"label": "Total", "amount": str(q.total)})
    return q
