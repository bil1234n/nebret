"""Company workspace API, Equb money collected by a company, late joiners, the member profile (KYC) and PIN sign-in."""
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from apps.agencies.services import add_staff
from apps.core.testutils import client_for, make_company, make_equb, make_listing, make_user, plenty_of_money
from apps.equb import services as equb
from apps.equb.models import Contribution, EqubMembership, EqubRound


def _equb():
    return make_equb(name="Car Equb", contribution_amount=Decimal("5000"), max_members=3,
                     start_date=timezone.localdate(), duration_value=3, duration_unit="MONTHS")


def _photo(name="me.jpg"):
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (20, 90, 60)).save(buf, "JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


class CompanyApiTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.listing = make_listing(company=self.company, units=2)
        self.owner = add_staff(self.company, full_name="Abel Owner", phone="0911000999", password="445566",
                               role="OWNER").user
        self.api = client_for(self.owner)

    def test_staff_sign_in_tells_the_app_it_is_a_company(self):
        r = APIClient().post("/api/auth/login/", {"phone": "0911000999", "password": "445566"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["user"]["company"]["name"], self.company.name)
        self.assertTrue(r.data["user"]["company"]["can_manage"])
        self.assertIsNone(client_for(make_user("Plain")).get("/api/auth/me/").data["company"])

    def test_customers_cannot_open_the_workspace(self):
        self.assertEqual(client_for(make_user("Plain")).get("/api/company/dashboard/").status_code, 403)

    def test_dashboard_listings_and_stock(self):
        d = self.api.get("/api/company/dashboard/").data
        self.assertEqual(d["counts"]["listings"], 1)
        self.assertEqual(d["counts"]["units"], 2)
        rows = self.api.get("/api/company/listings/").data
        self.assertEqual(rows["count"], 1)
        pk = rows["results"][0]["id"]
        r = self.api.post(f"/api/company/listings/{pk}/", {"action": "add_units", "count": 3}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["stock"]["AVAILABLE"], 5)
        r = self.api.patch(f"/api/company/listings/{pk}/", {"price": "2500000"}, format="json")
        self.assertEqual(Decimal(r.data["price"]), Decimal("2500000"))
        r = self.api.post("/api/company/listings/", {
            "category": self.listing.category.slug, "title": "Studio, Kazanchis", "summary": "Bright studio",
            "price": "1800000", "units": 2, "allow_reservation": False}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["stock"]["AVAILABLE"], 2)

    def test_sell_record_payment_and_hand_over(self):
        c = self.api.post("/api/company/customers/", {"full_name": "Mahi Kebede", "phone": "0976717113",
                                                      "pin": "482913"}, format="json")
        self.assertEqual(c.status_code, 201, c.data)
        o = self.api.post("/api/company/sell/", {"customer": c.data["id"], "listing": self.listing.pk, "plan": "FULL"},
                          format="json")
        self.assertEqual(o.status_code, 201, o.data)
        ref = o.data["reference"]
        self.assertTrue(o.data["payment_code"])
        self.assertTrue(o.data["customer"]["phone"].startswith("••") and o.data["customer"]["phone"].endswith("13"))  # staff never see the buyer's number
        # Office cash is off for marketplace orders – the buyer pays into the company's account by bank transfer.
        r = self.api.post(f"/api/company/orders/{ref}/", {"action": "payment", "method": "cash",
                                                          "amount": o.data["total_amount"]}, format="json")
        self.assertEqual(r.status_code, 409)
        # The buyer brings the receipt; staff upload it for them …
        from django.utils import timezone as tz

        from apps.market.models import BankTransferProof
        from apps.market.services import bank_transfer
        from apps.payments.verifiers import FakeVerifier

        when = tz.now()
        acc = o.data["pay_to_account"]["account_number"]
        FakeVerifier.register("CBE", "FT26111ABCDE", amount=o.data["total_amount"], payer="Mahi Kebede",
                              receiver_account=acc, transferred_at=when)
        pdf = SimpleUploadedFile("r.pdf", b"%PDF-1.4 receipt", content_type="application/pdf")
        r = self.api.post(f"/api/company/orders/{ref}/", {
            "action": "proof", "file": pdf, "bank": "CBE", "reference": "FT26111ABCDE", "amount": o.data["total_amount"],
            "transfer_datetime": when.isoformat(), "payer_name": "Mahi Kebede"}, format="multipart")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["proofs"][0]["status"], "SUBMITTED")
        # … the bank check runs in the background …
        bank_transfer.verify_due_proofs()
        queue = self.api.get("/api/company/payments/?tab=to_confirm").data
        self.assertEqual(queue["count"], 1)
        pid = queue["results"][0]["id"]
        # … and the owner confirms what the statement shows.
        r = self.api.post(f"/api/company/payments/{pid}/", {
            "action": "confirm", "amount": o.data["total_amount"], "payer_name": "MAHI KEBEDE",
            "credited_at": when.isoformat(), "statement_ref": "ST-001"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "CONFIRMED")
        r = self.api.get(f"/api/company/orders/{ref}/")
        self.assertEqual(r.data["status"], "PAID")
        self.assertEqual(len(r.data["earnings"]), 1)
        self.assertEqual(r.data["earnings"][0]["payout_status"], "RECEIVABLE")
        self.assertEqual(BankTransferProof.objects.get().status, "CONFIRMED")
        r = self.api.post("/api/company/handover/check/", {"payload": ref}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        r = self.api.post(f"/api/company/orders/{ref}/", {"action": "handover"}, format="json")
        self.assertEqual(r.data["status"], "COMPLETED")
        self.assertEqual(self.api.get("/api/company/orders/?tab=completed").data["count"], 1)
        self.assertEqual(self.api.get("/api/company/earnings/").data["count"], 1)
        # the new customer signs in to the app with the PIN the company gave them
        r = APIClient().post("/api/auth/login/", {"phone": "0976717113", "password": "482913"}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_enrol_a_customer_and_collect_their_equb_payment(self):
        g = _equb()
        c = self.api.post("/api/company/customers/", {"full_name": "Mahi Kebede", "phone": "0976717113"},
                          format="json").data
        place = self.api.post(f"/api/company/equbs/{g.slug}/enrol/", {"customer": c["id"]}, format="json")
        self.assertEqual(place.status_code, 201, place.data)
        equb.start_group(g)
        r = self.api.get(f"/api/company/equb-places/{place.data['id']}/")
        self.assertTrue(r.data["plan"]["options"])
        r = self.api.post(f"/api/company/equb-places/{place.data['id']}/", {"method": "cash", "rounds": 1}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["total_paid"]), g.contribution_amount)
        self.assertEqual(r.data["standing"]["paid_rounds"], 1)
        d = self.api.get("/api/company/equbs/").data
        self.assertEqual(len(d["places"]), 1)
        self.assertEqual(Decimal(d["collected"]), g.contribution_amount)
        # the web portal shows the same place and takes payments too
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f"/company/equb/place/{place.data['id']}/").status_code, 200)
        self.assertEqual(self.client.get(f"/company/equb/{g.slug}/enrol/").status_code, 200)

    def test_inquiry_reply(self):
        buyer = client_for(make_user("Buyer"))
        buyer.post(f"/api/market/listings/{self.listing.slug}/inquire/", {"kind": "QUESTION", "message": "Still there?"},
                   format="json")
        rows = self.api.get("/api/company/inquiries/?status=OPEN").data
        self.assertEqual(rows["count"], 1)
        r = self.api.post("/api/company/inquiries/", {"inquiry": rows["results"][0]["id"], "reply": "Yes, 2 left."},
                          format="json")
        self.assertEqual(r.data["status"], "ANSWERED")


class LateJoinerTests(TestCase):
    def test_someone_who_joins_after_a_round_was_drawn_still_owes_it(self):
        g = _equb()
        first = make_user("First")
        m1 = equb.join_group(first, g, auto_start=False)
        equb.start_group(g)
        equb.record_office_payment(m1, make_user("Staff", staff=True), method="cash", rounds=1)
        r1 = g.rounds.get(number=1)
        with plenty_of_money():
            equb.run_draw(r1)
        g.refresh_from_db()
        self.assertEqual(EqubRound.objects.get(pk=r1.pk).status, EqubRound.Status.DRAWN)
        late = make_user("Late")
        m2 = equb.join_group(late, g)
        slips = Contribution.objects.filter(membership=m2).order_by("round__number")
        self.assertEqual([c.round.number for c in slips][:2], [1, 2])
        plan = equb.payment_plan(m2)
        self.assertEqual(Decimal(plan["owed"]), g.contribution_amount * 2)
        d = client_for(late).get(f"/api/equb/memberships/{m2.id}/").data
        self.assertNotIn("not_billed", [row["state"] for row in d["schedule"][:2]])
        self.assertEqual(d["rules_summary"], [])
        self.assertEqual(Contribution.objects.get(membership=m2, round=r1).late_fee, 0)


class KycTests(TestCase):
    def test_profile_form_is_needed_for_the_draw_after_the_set_number_of_payments(self):
        g = _equb()
        g.kyc_after_payments = 1
        g.save()
        user = make_user("Member")
        m = equb.join_group(user, g, auto_start=False)
        equb.start_group(g)
        api = client_for(user)
        d = api.get(f"/api/equb/memberships/{m.id}/").data
        self.assertFalse(d["kyc"]["due"])
        equb.record_office_payment(m, make_user("Staff", staff=True), method="cash", rounds=1)
        d = api.get(f"/api/equb/memberships/{m.id}/").data
        self.assertTrue(d["kyc"]["due"])
        self.assertFalse(d["current_round"]["me"]["eligible"])
        self.assertIn(equb.KYC_REASON, d["current_round"]["me"]["reasons"])
        r = api.post("/api/auth/kyc/", {"photo": _photo(), "national_id": "ET-123456", "monthly_income": "25000"},
                     format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["profile"]["status"], "SUBMITTED")
        d = api.get(f"/api/equb/memberships/{m.id}/").data
        self.assertFalse(d["kyc"]["due"])
        self.assertTrue(d["current_round"]["me"]["eligible"])
        self.assertEqual(api.get("/api/auth/me/").data["kyc_status"], "SUBMITTED")

    def test_bad_form_is_refused(self):
        api = client_for(make_user("Member"))
        r = api.post("/api/auth/kyc/", {"national_id": "ET-1", "monthly_income": "100"}, format="multipart")
        self.assertEqual(r.status_code, 409, r.data)


class PinTests(TestCase):
    def test_register_with_a_pin_and_check_phone(self):
        api = APIClient()
        self.assertFalse(api.post("/api/auth/check-phone/", {"phone": "0912000111"}, format="json").data["registered"])
        bad = api.post("/api/auth/register/", {"phone": "0912000111", "full_name": "New Person", "password": "secret-pass"},
                       format="json")
        self.assertEqual(bad.status_code, 400)
        easy = api.post("/api/auth/register/", {"phone": "0912000111", "full_name": "New Person", "password": "111111"},
                        format="json")
        self.assertEqual(easy.status_code, 400)
        ok = api.post("/api/auth/register/", {"phone": "0912000111", "full_name": "New Person", "password": "482913"},
                      format="json")
        self.assertEqual(ok.status_code, 201, ok.data)
        r = api.post("/api/auth/check-phone/", {"phone": "912000111"}, format="json").data
        self.assertTrue(r["registered"])
        self.assertEqual(r["first_name"], "New")
        me = APIClient()
        me.credentials(HTTP_AUTHORIZATION="Bearer " + ok.data["tokens"]["access"])
        self.assertEqual(me.post("/api/auth/change-pin/", {"current": "482913", "new": "739415"}, format="json").status_code, 200)
        self.assertEqual(api.post("/api/auth/login/", {"phone": "0912000111", "password": "739415"}, format="json").status_code, 200)
