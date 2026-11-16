"""Shared fixtures for the test-suite."""
from datetime import timedelta
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


def make_company(name="Sunshine Real Estate", kind="REAL_ESTATE", bank_account=True, **kw):
    """A partner company. By default it has an approved, usable bank account (buyers pay straight into it)."""
    from apps.agencies.models import Agency

    agency = Agency.objects.create(name=name, kind=kind, phone="+251911000000", **kw)
    if bank_account:
        make_bank_account(agency)
    return agency


def make_bank_account(agency, number=None, currency="ETB", bank="CBE"):
    from django.utils import timezone

    from apps.agencies.models import PartnerBankAccount

    _counter["n"] += 1
    return PartnerBankAccount.objects.create(
        agency=agency, bank=bank, account_name=agency.name, account_number=number or f"1000{_counter['n']:09d}",
        currency=currency, status=PartnerBankAccount.Status.APPROVED, is_active=True, is_verified=True,
        verified_at=timezone.now() - timedelta(days=2), usable_from=timezone.now() - timedelta(days=1))


def make_listing(price=Decimal("3000000"), units=3, company=None, category=None, **kw):
    """A live listing with ``units`` units in stock. Every payment plan is on by default."""
    from apps.market.models import Listing

    defaults = dict(title="3 bedroom apartment, Bole", summary="145 m², 4th floor", price=price,
                    allow_full_payment=True, allow_reservation=True, reservation_fee=Decimal("50000"),
                    reservation_days=14, allow_installments=True, min_down_payment_percent=Decimal("30"),
                    max_installment_months=24, finance_rate_percent=Decimal("0"),
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


HOUSE_ACCOUNT = {"bank": "CBE", "account_name": "Nebret Equb Sales", "account_number": "1000555000111",
                 "currency": "ETB", "branch": "Bole", "swift_code": ""}


def bank_pay(order, amount=None, *, payer=None, reference=None, when=None, confirmer=None, bank="CBE"):
    """
    Pay (part of) an order the way buyers now do: upload a receipt, the bank verifies it (fake verifier), the seller
    confirms it. Returns the confirmed proof.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.utils import timezone

    from apps.core.utils import generate_code
    from apps.market.models import Order
    from apps.market.services import bank_transfer
    from apps.payments.verifiers import FakeVerifier

    order = Order.objects.get(pk=order.pk)
    amount = Decimal(str(amount if amount is not None else order.amount_due_now))
    payer = payer or order.buyer_name or order.user.full_name
    reference = reference or ("FT" + generate_code(10))
    when = when or timezone.now()
    FakeVerifier.register(bank, reference, amount=str(amount), payer=payer,
                          receiver_account=order.pay_to_account["account_number"], transferred_at=when,
                          narration=f"Payment {order.payment_code}")
    receipt = SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test receipt", content_type="application/pdf")
    proof = bank_transfer.submit_proof(order, order.user, file=receipt, bank=bank, reference=reference, amount=amount,
                                       transfer_datetime=when, payer_name=payer)
    proof = bank_transfer.verify_proof(proof)
    assert proof.status == "VERIFIED_AWAITING_PARTNER", (proof.status, proof.rejection_reason, proof.shegerpay_response)
    if confirmer is None:
        confirmer = company_owner(order.company) if order.company_id else make_user("Nebret staff", staff=True)
    proof = bank_transfer.confirm_proof(proof, confirmer, amount=amount, payer_name=payer, credited_at=when,
                                        statement_ref="ST" + reference[-6:], note="test", evidence=None
                                        if order.company_id else None)
    return proof


def company_owner(agency):
    from apps.agencies.models import AgencyStaff

    st = AgencyStaff.objects.filter(agency=agency, role=AgencyStaff.Role.OWNER, is_active=True).first()
    if st:
        return st.user
    from apps.agencies.services import add_staff

    _counter["n"] += 1
    return add_staff(agency, full_name="Owner Person", phone=f"0911{_counter['n']:06d}", role="OWNER").user
