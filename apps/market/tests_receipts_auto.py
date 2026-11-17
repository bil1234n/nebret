"""Receipts are checked with the bank automatically: on upload, without a typed reference, and from the photo."""
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.agencies import commission as cm
from apps.agencies.models import AgencyLedgerEntry, CommissionPayment
from apps.core.testutils import client_for, company_owner, make_company, make_listing, make_user
from apps.payments.models import BankReference
from apps.payments.verifiers import FakeVerifier, ShegerPayVerifier, VerificationRequest

from .models import BankTransferProof
from .services import bank_transfer, orders

P = BankTransferProof.Status
AUTO = {**settings.NEBRET, "VERIFY_ON_SUBMIT": True, "SHEGERPAY_RECEIVER_CHECK": "merchant_name"}
PHOTO = b"\xff\xd8\xff\xe0 receipt photo FT26321J8RFR"


def photo(data=PHOTO):
    return SimpleUploadedFile("r.jpg", data, content_type="image/jpeg")


@override_settings(NEBRET=AUTO)
class AutoVerifyTests(TestCase):
    def setUp(self):
        FakeVerifier.reset()
        self.company = make_company()
        self.owner = company_owner(self.company)
        self.listing = make_listing(company=self.company, units=2, price=Decimal("2"))
        self.buyer = make_user("Mahi")
        self.order = orders.create_order(user=self.buyer, listing=self.listing, plan="FULL")

    def test_verified_by_bank_on_upload_without_reference(self):
        FakeVerifier.register_image(PHOTO, "CBE", "FT26321J8RFR", amount="2", payer="Mahi")
        proof = bank_transfer.submit_proof(self.order, self.buyer, file=photo(), bank="CBE", reference="",
                                           amount=2, transfer_datetime=timezone.now(), payer_name="Mahi")
        proof.refresh_from_db()
        self.assertEqual(proof.status, P.VERIFIED_AWAITING_PARTNER)
        self.assertEqual(proof.normalised_reference, "FT26321J8RFR")
        self.assertTrue(BankReference.objects.filter(bank="CBE", reference="FT26321J8RFR").exists())
        # the same receipt can't pay a second order
        other = orders.create_order(user=self.buyer, listing=self.listing, plan="FULL")
        again = bank_transfer.submit_proof(other, self.buyer, file=photo(), bank="CBE", reference="", amount=2,
                                           transfer_datetime=timezone.now(), payer_name="Mahi")
        again.refresh_from_db()
        self.assertEqual(again.status, P.REJECTED)
        self.assertIn("already used", again.rejection_reason)

    def test_receiver_not_returned_is_fine_when_shegerpay_checked_the_merchant_name(self):
        FakeVerifier.register("CBE", "FT9988776655", amount="2", payer="Mahi")  # no receiver account returned
        proof = bank_transfer.submit_proof(self.order, self.buyer, file=photo(b"\xff\xd8\xff other"), bank="CBE",
                                           reference="FT9988776655", amount=2, transfer_datetime=timezone.now(),
                                           payer_name="Mahi")
        proof.refresh_from_db()
        self.assertEqual(proof.status, P.VERIFIED_AWAITING_PARTNER)

    def test_unreadable_now_is_retried_when_the_order_is_opened(self):
        FakeVerifier.outage = True
        proof = bank_transfer.submit_proof(self.order, self.buyer, file=photo(), bank="CBE", reference="",
                                           amount=2, transfer_datetime=timezone.now(), payer_name="Mahi")
        proof.refresh_from_db()
        self.assertEqual(proof.status, P.VERIFYING)
        FakeVerifier.outage = False
        FakeVerifier.register_image(PHOTO, "CBE", "FT26321J8RFR", amount="2", payer="Mahi")
        BankTransferProof.objects.filter(pk=proof.pk).update(next_check_at=timezone.now())
        r = client_for(self.buyer).get(f"/api/market/orders/{self.order.reference}/")
        self.assertEqual(r.status_code, 200)
        proof.refresh_from_db()
        self.assertEqual(proof.status, P.VERIFIED_AWAITING_PARTNER)

    def test_cron_endpoint_needs_the_secret(self):
        with override_settings(CRON_SECRET="s3cret"):
            self.assertEqual(self.client.get("/api/cron/tick/").status_code, 403)
            r = self.client.get("/api/cron/tick/?draws=0", HTTP_AUTHORIZATION="Bearer s3cret")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["ok"])


class ShegerPayImageTests(TestCase):
    def test_no_reference_uses_verify_image_with_screenshot(self):
        v = ShegerPayVerifier(api_key="sk_test_x", base_url="https://api.example")
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"valid": True, "verified": True, "status": "verified", "amount": 2,
                                  "transaction_id": "FT26321J8RFR", "payer": "Mahi"}
        with mock.patch("apps.payments.verifiers.requests.post", return_value=resp) as post:
            r = v.verify(VerificationRequest(channel="CBE", reference="", amount=Decimal("2"), receiver_account="1",
                                             receiver_name="Bilal Nesru Jemal", receipt=PHOTO))
        self.assertEqual(r.outcome, "FOUND")
        self.assertEqual(r.transaction_id, "FT26321J8RFR")
        url = post.call_args.args[0]
        self.assertTrue(url.endswith("/api/v1/verify-image"))
        self.assertIn("screenshot", post.call_args.kwargs["files"])
        self.assertEqual(post.call_args.kwargs["data"]["merchant_name"], "Bilal Nesru Jemal")

    def test_typed_reference_not_found_falls_back_to_the_photo(self):
        v = ShegerPayVerifier(api_key="sk_test_x", base_url="https://api.example")
        miss = mock.Mock(status_code=200)
        miss.json.return_value = {"valid": False, "status": "not_found", "reason": "not found"}
        hit = mock.Mock(status_code=200)
        hit.json.return_value = {"valid": True, "amount": 2, "transaction_id": "FT26321J8RFR"}
        with mock.patch("apps.payments.verifiers.requests.post", side_effect=[miss, hit]) as post:
            r = v.verify(VerificationRequest(channel="CBE", reference="FT26321J8RFX", amount=Decimal("2"),
                                             receiver_account="1", receiver_name="X", receipt=PHOTO))
        self.assertEqual(r.outcome, "FOUND")
        self.assertEqual([c.args[0].rsplit("/", 1)[1] for c in post.call_args_list], ["verify", "verify-image"])


@override_settings(NEBRET={**AUTO, "COMMISSION_BANK_ACCOUNT": {"bank": "CBE", "account_name": "Nebret Commission",
                                                               "account_number": "1000777766665", "currency": "ETB"}})
class CommissionPaymentTests(TestCase):
    def setUp(self):
        FakeVerifier.reset()
        self.company = make_company()
        self.owner = company_owner(self.company)
        self.buyer = make_user("Meron")
        for amount in ("50", "30"):
            AgencyLedgerEntry.objects.create(
                agency=self.company, kind="GATEWAY", direction="TO_NEBRET", customer=self.buyer,
                gross_amount=Decimal(amount) * 20, commission_percent=5, commission=Decimal(amount),
                agency_amount=Decimal(amount) * 19, payout_status="RECEIVABLE")

    def test_company_uploads_commission_receipt_and_it_is_collected(self):
        api = client_for(self.owner)
        self.assertEqual(api.get("/api/company/commission/").data["due"], "80.00")
        FakeVerifier.register_image(PHOTO, "CBE", "FT1122334455", amount="60")
        r = api.post("/api/company/commission/", {"file": photo(), "bank": "CBE", "amount": "60",
                                                  "transfer_datetime": timezone.now().isoformat()}, format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "VERIFIED")
        statuses = list(self.company.ledger.order_by("created_at").values_list("payout_status", flat=True))
        self.assertEqual(statuses, ["COLLECTED", "RECEIVABLE"])  # oldest (50) collected; 10 kept as credit
        self.assertEqual(cm.credit(self.company), Decimal("10.00"))
        self.assertEqual(api.get("/api/company/commission/").data["due"], "20.00")

    def test_wrong_amount_is_rejected(self):
        FakeVerifier.register_image(PHOTO, "CBE", "FT1122334455", amount="5")
        cp = cm.submit(self.company, self.owner, file=photo(), bank="CBE", amount="60",
                       transfer_datetime=timezone.now())
        self.assertEqual(cp.status, CommissionPayment.Status.REJECTED)
        self.assertFalse(self.company.ledger.filter(payout_status="COLLECTED").exists())
