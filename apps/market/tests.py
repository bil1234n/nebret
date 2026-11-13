"""Marketplace end-to-end: browse, quote, every payment plan, stock, installments, Equb payouts, handover, companies."""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.agencies.models import AgencyLedgerEntry
from apps.agencies.services import add_staff
from apps.core.exceptions import BusinessRuleError
from apps.core.testutils import client_for, make_company, make_listing, make_user

from .models import Installment, ListingUnit, Order
from .services import handover, operations, orders, pricing


def checkout(reference, result="SUCCESS"):
    r = APIClient().post(f"/api/payments/mock/checkout/{reference}/", {"result": result}, format="json")
    assert r.status_code == 200, r.data
    return r.data


class CatalogueTests(TestCase):
    def setUp(self):
        self.listing = make_listing()
        self.api = APIClient()

    def test_browse_filter_and_detail(self):
        r = self.api.get("/api/market/listings/?group=HOMES&plan=INSTALLMENT")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        row = r.data["results"][0]
        self.assertEqual(row["units_available"], 3)
        self.assertIn("INSTALLMENT", row["payment_plans"])
        self.assertIsNotNone(row["monthly_from"])
        self.assertEqual(self.api.get("/api/market/listings/?group=VEHICLES").data["count"], 0)
        d = self.api.get(f"/api/market/listings/{self.listing.slug}/").data
        self.assertEqual(len(d["units"]), 3)
        self.assertIn("Bedrooms", [row["label"] for row in d["specs"]])

    def test_categories_and_home(self):
        self.assertEqual(self.api.get("/api/market/categories/").data[0]["listing_count"], 1)
        home = self.api.get("/api/home/dashboard/").data
        self.assertEqual(len(home["new_listings"]), 1)

    def test_quote_installments(self):
        r = self.api.post(f"/api/market/listings/{self.listing.slug}/quote/",
                          {"plan": "INSTALLMENT", "months": 12, "down_payment": "900000"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["first_payment"], "900000.00")
        self.assertEqual(r.data["monthly"], "175000.00")  # (3,000,000 − 900,000) / 12

    def test_quote_rejects_low_down_payment(self):
        r = self.api.post(f"/api/market/listings/{self.listing.slug}/quote/",
                          {"plan": "INSTALLMENT", "months": 12, "down_payment": "1000"}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_finance_charge_is_flat_and_fixed(self):
        self.listing.finance_rate_percent = Decimal("10")
        q = pricing.quote(self.listing, plan=Order.Plan.INSTALLMENT, months=12, down_payment=Decimal("1000000"))
        self.assertEqual(q.finance_charge, Decimal("200000.00"))  # 2,000,000 × 10% × 12/12
        self.assertEqual(q.total, Decimal("3200000.00"))


class OrderFlowTests(TestCase):
    def setUp(self):
        self.user = make_user("Meron Tadesse")
        self.api = client_for(self.user)
        self.listing = make_listing(units=1)

    def order(self, **data):
        r = self.api.post("/api/market/orders/", {"listing": self.listing.slug, **data}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def pay(self, reference, **data):
        r = self.api.post(f"/api/market/orders/{reference}/pay/", {"provider": "mock", **data}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return checkout(r.data["reference"])

    def test_full_payment_then_handover(self):
        o = self.order(plan="FULL")
        self.assertEqual(o["amount_due_now"], "3000000.00")
        unit = ListingUnit.objects.get()
        self.assertEqual(unit.status, ListingUnit.Status.HELD)
        self.pay(o["reference"])
        order = Order.objects.get(reference=o["reference"])
        self.assertEqual(order.status, Order.Status.PAID)
        unit.refresh_from_db()
        self.assertEqual(unit.status, ListingUnit.Status.SOLD)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, "SOLD_OUT")
        detail = self.api.get(f"/api/market/orders/{order.reference}/").data
        self.assertTrue(detail["handover_pass"]["payload"].startswith("NEBRET:"))
        self.assertEqual(handover.verify_pass(detail["handover_pass"]["payload"]), order)
        self.assertIsNone(handover.verify_pass(f"NEBRET:{order.reference}:FORGED"))
        orders.complete_handover(order, staff=make_user("Staff", staff=True))
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)

    def test_sold_out_unit_cannot_be_ordered_twice(self):
        self.order(plan="FULL")
        other = client_for(make_user("Other"))
        r = other.post("/api/market/orders/", {"listing": self.listing.slug, "plan": "FULL"}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_reservation_then_balance(self):
        o = self.order(plan="RESERVATION")
        self.assertEqual(o["first_payment"], "50000.00")
        self.pay(o["reference"])
        order = Order.objects.get(reference=o["reference"])
        self.assertEqual(order.status, Order.Status.RESERVED)
        self.assertEqual(order.balance_due, Decimal("2950000"))
        self.assertEqual(order.reserved_until, timezone.localdate() + timedelta(days=14))
        self.pay(o["reference"])
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_installments_schedule_and_payoff(self):
        o = self.order(plan="INSTALLMENT", months=6, down_payment="900000")
        self.pay(o["reference"])
        order = Order.objects.get(reference=o["reference"])
        self.assertEqual(order.status, Order.Status.INSTALLMENTS)
        self.assertEqual(order.installments.count(), 6)
        self.assertEqual(sum(i.amount for i in order.installments.all()), Decimal("2100000"))
        self.pay(o["reference"], count=2)
        self.assertEqual(order.installments.filter(status="PAID").count(), 2)
        self.pay(o["reference"], count=4)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.balance_due, 0)

    def test_late_installment_fee_and_waiver(self):
        o = self.order(plan="INSTALLMENT", months=3, down_payment="900000")
        self.pay(o["reference"])
        order = Order.objects.get(reference=o["reference"])
        first = order.installments.get(number=1)
        Installment.objects.filter(pk=first.pk).update(due_date=timezone.localdate() - timedelta(days=30))
        out = orders.sweep_installments()
        self.assertEqual(out["late"], 1)
        first.refresh_from_db()
        self.assertEqual(first.status, Installment.Status.LATE)
        self.assertEqual(first.late_fee, Decimal("14000.00"))  # 2% of 700,000
        operations.waive_installment_fee(first, make_user("Staff", staff=True), "bank outage")
        first.refresh_from_db()
        self.assertEqual(first.late_fee, 0)

    def test_unpaid_order_expires_and_unit_returns(self):
        o = self.order(plan="FULL")
        Order.objects.filter(reference=o["reference"]).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(orders.expire_unpaid_orders(), 1)
        self.assertEqual(ListingUnit.objects.get().status, ListingUnit.Status.AVAILABLE)

    def test_customer_cancels_unpaid_order(self):
        o = self.order(plan="FULL")
        r = self.api.post(f"/api/market/orders/{o['reference']}/cancel/", {}, format="json")
        self.assertEqual(r.data["status"], "CANCELLED")
        self.assertEqual(ListingUnit.objects.get().status, ListingUnit.Status.AVAILABLE)

    def test_paid_order_cancel_and_refund_by_staff(self):
        o = self.order(plan="RESERVATION")
        self.pay(o["reference"])
        order = Order.objects.get(reference=o["reference"])
        with self.assertRaises(BusinessRuleError):
            orders.cancel_order(order)  # customers can't cancel a paid reservation themselves
        staff = make_user("Staff", staff=True)
        orders.cancel_order(order, "Customer changed mind", staff=staff)
        operations.refund_order(order, staff, amount=Decimal("50000"), method="Bank transfer", reference="TRX1")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)

    def test_office_payment(self):
        o = self.order(plan="FULL")
        order = Order.objects.get(reference=o["reference"])
        staff = make_user("Staff", staff=True)
        with self.assertRaises(BusinessRuleError):
            operations.record_office_payment(order, staff, amount=1000, method="bank")  # needs a reference
        operations.record_office_payment(order, staff, amount=order.total_amount, method="cash")
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)

    def test_saved_listings_and_inquiry(self):
        self.api.post(f"/api/market/listings/{self.listing.slug}/save/")
        self.assertEqual(len(self.api.get("/api/market/listings/saved/").data), 1)
        r = self.api.post(f"/api/market/listings/{self.listing.slug}/inquire/",
                          {"kind": "VIEWING", "message": "Can I see it on Saturday?"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)


class EqubPayoutTests(TestCase):
    def test_payout_used_on_an_order(self):
        from apps.core.testutils import make_equb
        from apps.equb.models import EqubMembership, EqubPayout, EqubRound

        user = make_user("Winner")
        listing = make_listing(price=Decimal("68000"), units=2)
        group = make_equb(name="E-bike Equb", contribution_amount=Decimal("6000"), max_members=2,
                          start_date=timezone.localdate(), duration_value=12, duration_unit="WEEKS")
        m = EqubMembership.objects.create(group=group, user=user, position=1)
        rnd = EqubRound.objects.create(group=group, number=1, due_date=timezone.localdate(),
                                       draw_date=timezone.localdate())
        payout = EqubPayout.objects.create(round=rnd, membership=m, amount=Decimal("70000"), voucher_code="EQTEST01")
        api = client_for(user)
        o = api.post("/api/market/orders/", {"listing": listing.slug, "plan": "EQUB_PAYOUT"}, format="json").data
        r = api.post(f"/api/market/orders/{o['reference']}/use-payout/", {"voucher_code": "EQTEST01"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "PAID")
        payout.refresh_from_db()
        self.assertEqual(payout.status, "REDEEMED")
        self.assertEqual(payout.order.reference, o["reference"])


class CompanyTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.listing = make_listing(company=self.company, units=2)
        self.owner = add_staff(self.company, full_name="Abel Owner", phone="0911000999", password="company123",
                               role="OWNER").user

    def test_company_sale_records_commission(self):
        user = make_user("Buyer")
        api = client_for(user)
        o = api.post("/api/market/orders/", {"listing": self.listing.slug, "plan": "FULL"}, format="json").data
        txn = api.post(f"/api/market/orders/{o['reference']}/pay/", {"provider": "mock"}, format="json").data
        checkout(txn["reference"])
        entry = AgencyLedgerEntry.objects.get()
        self.assertEqual(entry.gross_amount, Decimal("3000000"))
        self.assertEqual(entry.commission, Decimal("150000.00"))
        self.assertEqual(entry.order.reference, o["reference"])

    def test_company_portal_pages(self):
        self.client.force_login(self.owner)
        for name in ("dashboard", "listings", "orders", "handover", "inquiries", "customers", "equbs", "earnings",
                     "team", "profile", "sell", "listing_add"):
            self.assertEqual(self.client.get(reverse(f"agency:{name}")).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("agency:listing", args=[self.listing.pk])).status_code, 200)


class ConsolePageTests(TestCase):
    def test_every_marketplace_console_page_loads(self):
        staff = make_user("Admin", staff=True)
        self.client.force_login(staff)
        listing = make_listing()
        order = orders.create_order(user=make_user("Buyer"), listing=listing, plan=Order.Plan.FULL)
        pages = ["dashboard", "orders", "installments", "handover", "listings", "listing_add", "inquiries", "agencies",
                 "agency_add", "settings", "category_add", "users", "payments", "sell", "equbs", "community"]
        for name in pages:
            self.assertEqual(self.client.get(reverse(f"console:{name}")).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("console:order", args=[order.reference])).status_code, 200)
        self.assertEqual(self.client.get(reverse("console:listing", args=[listing.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("console:orders") + "?export=csv").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
