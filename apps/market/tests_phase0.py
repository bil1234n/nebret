"""Phase 0 fixes: units are never released while money is paid or a receipt is in review; the office-payment ledger
direction; transfer references can be used only once."""
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.agencies.models import AgencyLedgerEntry
from apps.agencies.services import add_staff, summary
from apps.core.exceptions import BusinessRuleError
from apps.core.testutils import make_company, make_listing, make_user
from apps.payments.models import BankReference, FraudAlert, PaymentTransaction
from apps.payments.references import DuplicateReference, normalise_reference

from .models import ListingUnit, Order
from .services import operations, orders


def _expire(order):
    Order.objects.filter(pk=order.pk).update(expires_at=timezone.now() - timedelta(minutes=5))
    ListingUnit.objects.filter(pk=order.unit_id).update(held_until=timezone.now() - timedelta(minutes=5))


class UnitHoldTests(TestCase):
    def setUp(self):
        self.listing = make_listing(units=1)
        self.buyer = make_user("Buyer")

    def order(self, plan="RESERVATION"):
        return orders.create_order(user=self.buyer, listing=self.listing, plan=plan)

    def test_unit_kept_while_order_has_money_paid(self):
        o = self.order(plan="INSTALLMENT")
        Order.objects.filter(pk=o.pk).update(amount_paid=Decimal("1000"))  # part of the down payment
        _expire(o)
        self.assertEqual(orders.expire_unpaid_orders(), 0)
        self.assertEqual(orders.release_stale_holds(), 0)
        self.assertEqual(ListingUnit.objects.get().status, ListingUnit.Status.HELD)
        o.refresh_from_db()
        self.assertEqual(o.status, Order.Status.PENDING_PAYMENT)
        # and nobody else can grab the unit through the "expired hold" path
        with self.assertRaises(BusinessRuleError):
            orders.create_order(user=make_user("Other"), listing=self.listing, plan="FULL", unit_id=o.unit_id)

    def test_unit_kept_while_receipt_in_review(self):
        o = self.order()
        PaymentTransaction.objects.create(user=self.buyer, provider="bank", purpose="ORDER", amount=o.first_payment,
                                          status=PaymentTransaction.Status.PENDING, target=o)
        _expire(o)
        self.assertEqual(orders.expire_unpaid_orders(), 0)
        self.assertEqual(orders.release_stale_holds(), 0)
        self.assertEqual(ListingUnit.objects.get().status, ListingUnit.Status.HELD)
        with self.assertRaises(BusinessRuleError):
            orders.create_order(user=make_user("Other"), listing=self.listing, plan="FULL", unit_id=o.unit_id)

    def test_abandoned_unpaid_hold_is_still_released(self):
        o = self.order()
        PaymentTransaction.objects.create(user=self.buyer, provider="bank", purpose="ORDER", amount=o.first_payment,
                                          status=PaymentTransaction.Status.FAILED, target=o)  # rejected receipt
        _expire(o)
        self.assertEqual(orders.expire_unpaid_orders(), 1)
        self.assertEqual(ListingUnit.objects.get().status, ListingUnit.Status.AVAILABLE)

    def test_orphan_stale_hold_released_and_unit_can_be_bought(self):
        o = self.order()
        _expire(o)
        # the order is unpaid and past its window: another buyer may take the unit straight away
        other = orders.create_order(user=make_user("Other"), listing=self.listing, plan="FULL", unit_id=o.unit_id)
        self.assertEqual(other.unit_id, o.unit_id)


OFFICE_ON = override_settings(NEBRET={**settings.NEBRET, "MARKET_OFFICE_PAYMENTS": True})


@OFFICE_ON
class OfficePaymentLedgerTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.listing = make_listing(company=self.company, units=3, price=Decimal("100000"))
        self.owner = add_staff(self.company, full_name="Owner", phone="0911000777", role="OWNER").user
        self.nebret = make_user("Nebret Staff", staff=True)
        self.buyer = make_user("Buyer")

    def order(self):
        return orders.create_order(user=self.buyer, listing=self.listing, plan="FULL")

    def test_company_staff_payment_means_company_owes_commission(self):
        o = self.order()
        operations.record_office_payment(o, self.owner, amount=o.total_amount, method="cash")
        e = AgencyLedgerEntry.objects.get(order=o)
        self.assertEqual(e.direction, AgencyLedgerEntry.Direction.TO_NEBRET)
        self.assertEqual(e.payout_status, AgencyLedgerEntry.Payout.RECEIVABLE)
        self.assertEqual(e.commission, Decimal("5000.00"))
        s = summary(self.company)
        self.assertEqual(s["owed"], 0)
        self.assertEqual(s["receivable"], Decimal("5000.00"))
        self.assertEqual(s["net"], Decimal("-5000.00"))

    def test_nebret_staff_defaults_to_company_received(self):
        o = self.order()
        operations.record_office_payment(o, self.nebret, amount=o.total_amount, method="bank", reference="FT1")
        self.assertEqual(AgencyLedgerEntry.objects.get(order=o).payout_status, AgencyLedgerEntry.Payout.RECEIVABLE)

    def test_nebret_staff_can_record_money_nebret_received(self):
        o = self.order()
        operations.record_office_payment(o, self.nebret, amount=o.total_amount, method="cash", paid_to="nebret")
        e = AgencyLedgerEntry.objects.get(order=o)
        self.assertEqual(e.direction, AgencyLedgerEntry.Direction.TO_COMPANY)
        self.assertEqual(e.payout_status, AgencyLedgerEntry.Payout.OWED)
        self.assertEqual(summary(self.company)["owed"], Decimal("95000.00"))

    def test_company_staff_cannot_claim_nebret_received(self):
        o = self.order()
        operations.record_office_payment(o, self.owner, amount=o.total_amount, method="cash", paid_to="nebret")
        self.assertEqual(AgencyLedgerEntry.objects.get(order=o).direction, AgencyLedgerEntry.Direction.TO_NEBRET)

    def test_refund_reverses_receivable(self):
        o = self.order()
        operations.record_office_payment(o, self.owner, amount=o.total_amount, method="cash")
        orders.cancel_order(o, "Changed mind", staff=self.nebret)
        operations.refund_order(o, self.nebret, amount=o.total_amount, method="Bank transfer", reference="R1")
        self.assertEqual(AgencyLedgerEntry.objects.get(order=o).payout_status, AgencyLedgerEntry.Payout.REVERSED)
        self.assertEqual(summary(self.company)["receivable"], 0)


@OFFICE_ON
class DuplicateReferenceTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.other_company = make_company(name="Other Motors", kind="CAR_DEALER")
        self.listing = make_listing(company=self.company, units=3, price=Decimal("100000"))
        self.other_listing = make_listing(company=self.other_company, units=1, price=Decimal("50000"))
        self.staff = make_user("Nebret Staff", staff=True)

    def order(self, listing=None):
        return orders.create_order(user=make_user("Buyer"), listing=listing or self.listing, plan="FULL")

    def test_normalise(self):
        self.assertEqual(normalise_reference(" ft 24-123/abc "), "FT24123ABC")

    def test_reference_reused_on_another_order_and_company_is_refused_and_alerted(self):
        a = self.order()
        operations.record_office_payment(a, self.staff, amount=1000, method="bank", reference="FT24123ABC", bank="CBE")
        b = self.order(self.other_listing)
        with self.assertRaises(DuplicateReference) as ctx:
            operations.record_office_payment(b, self.staff, amount=1000, method="bank", reference="ft 24123-abc",
                                             bank="cbe")
        self.assertIn("already been used", str(ctx.exception.detail))
        b.refresh_from_db()
        self.assertEqual(b.amount_paid, 0)  # nothing was applied
        self.assertFalse(PaymentTransaction.objects.filter(object_id=b.pk, provider="bank").exists())
        alert = FraudAlert.objects.get()  # kept although the payment was rolled back
        self.assertEqual(alert.order_reference, b.reference)
        self.assertIn(a.reference, alert.message)
        self.assertTrue(self.staff.notifications.filter(title__startswith="Fraud alert").exists())

    def test_same_order_twice_refused(self):
        a = self.order()
        operations.record_office_payment(a, self.staff, amount=1000, method="bank", reference="FT9")
        with self.assertRaises(DuplicateReference):
            operations.record_office_payment(a, self.staff, amount=1000, method="bank", reference="FT9")

    def test_unknown_bank_clashes_with_named_bank(self):
        operations.record_office_payment(self.order(), self.staff, amount=1000, method="bank", reference="FT77",
                                         bank="Dashen")
        with self.assertRaises(DuplicateReference):
            operations.record_office_payment(self.order(), self.staff, amount=1000, method="bank", reference="FT77")

    def test_different_banks_and_mobile_are_separate(self):
        operations.record_office_payment(self.order(), self.staff, amount=1000, method="bank", reference="X1",
                                         bank="CBE")
        operations.record_office_payment(self.order(), self.staff, amount=1000, method="bank", reference="X1",
                                         bank="AWASH")
        operations.record_office_payment(self.order(), self.staff, amount=1000, method="mobile", reference="X1")
        self.assertEqual(BankReference.objects.count(), 3)

    def test_database_constraint(self):
        BankReference.objects.create(bank="CBE", reference="FT1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            BankReference.objects.create(bank="CBE", reference="FT1")
