"""Shared fixtures for the test-suite."""
from decimal import Decimal

from rest_framework.test import APIClient

from apps.accounts.models import User

_counter = {"n": 0}


def make_user(name="Test User", staff=False, **extra):
    _counter["n"] += 1
    return User.objects.create_user(phone=f"+2519{_counter['n']:08d}", password="pass12345", full_name=name,
                                    is_staff=staff, **extra)


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def make_category(name="Apartments", group="HOMES", **kw):
    from apps.market.models import Category

    return Category.objects.get_or_create(name=name, defaults={"group": group, **kw})[0]


def make_company(name="Sunshine Real Estate", kind="REAL_ESTATE", **kw):
    from apps.agencies.models import Agency

    return Agency.objects.create(name=name, kind=kind, phone="+251911000000", **kw)


def make_listing(price=Decimal("3000000"), units=3, company=None, category=None, **kw):
    """A live listing with ``units`` units in stock. Every payment plan is on by default."""
    from apps.market.models import Listing

    defaults = dict(title="3 bedroom apartment, Bole", summary="145 m², 4th floor", price=price,
                    allow_full_payment=True, allow_reservation=True, reservation_fee=Decimal("50000"),
                    reservation_days=14, allow_installments=True, min_down_payment_percent=Decimal("30"),
                    max_installment_months=24, finance_rate_percent=Decimal("0"), allow_equb_payout=True,
                    specs={"bedrooms": 3, "area_m2": 145})
    defaults.update(kw)
    listing = Listing.objects.create(category=category or make_category(), company=company, **defaults)
    if units:
        listing.add_units(units)
    return listing


RULE_FIELDS = ("service_fee_percent", "late_fee", "grace_days", "require_paid_to_draw", "payout_basis",
               "auto_start_when_full")
GONE_FIELDS = ("destination", "package", "description", "payout_type", "is_public", "kind", "image_url",
               "min_members_to_start", "winners_per_round")


def make_equb(**kw):
    """Create an Equb group. Money rules are company-wide now, so they go to the Equb rules (EqubSettings)."""
    from apps.equb.models import EqubGroup, EqubSettings

    rules = {k: kw.pop(k) for k in RULE_FIELDS if k in kw}
    for k in GONE_FIELDS:
        kw.pop(k, None)
    if rules:
        s = EqubSettings.load()
        for k, v in rules.items():
            setattr(s, k, v)
        s.save()
    return EqubGroup.objects.create(**kw)


def plenty_of_money():
    """
    Patch for tests about who can win (pools, selection, groups) rather than about money: pretend the Equb holds
    enough for 99 winners. The money rule itself is tested separately.
    """
    from unittest import mock

    def fake(group, rnd=None):
        from apps.equb.services import prize_for

        prize = prize_for(group, rnd)
        fee = (prize * Decimal(group.service_fee_percent) / 100).quantize(Decimal("0.01"))
        return {"collected": prize * 99, "paid_out": Decimal("0"), "available": prize * 99, "prize": prize, "fee": fee,
                "net": prize - fee, "max_winners": 99, "short_for_one": Decimal("0")}

    return mock.patch("apps.equb.services.equb_funds", new=fake)
