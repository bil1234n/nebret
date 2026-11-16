"""
Marketplace bank-transfer payments: receipts, ShegerPay verification, seller confirmation, dual control, disputes,
split transfers, commission, holds, SLA, agreements, product Equbs and the EQUB_PAYOUT removal.
"""
import threading
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.agencies.models import AgencyLedgerEntry, AgencyStaff, PartnerBankAccount
from apps.agencies.services import add_staff
from apps.core.business_days import add_business_days, business_days_between
from apps.core.exceptions import BusinessRuleError
from apps.core.testutils import (bank_pay, client_for, company_owner, make_company, make_equb, make_listing,
                                 make_user, plenty_of_money)
from apps.payments.models import BankReference, FraudAlert, PaymentTransaction
from apps.payments.references import DuplicateReference
from apps.payments.verifiers import FakeVerifier, ShegerPayVerifier

from .models import BankTransferProof, ListingUnit, Order, OrderAgreement
from .services import agreement, bank_transfer, orders

P = BankTransferProof.Status


def receipt(name="receipt.jpg"):
    return SimpleUploadedFile(name, b"\xff\xd8\xff\xe0 fake jpeg", content_type="image/jpeg")


class Base(TestCase):
    def setUp(self):
        FakeVerifier.reset()
        self.company = make_company()
        self.owner = company_owner(self.company)
        self.manager = add_staff(self.company, full_name="Mimi Manager", phone="0911777001", role="MANAGER").user
        self.agent = add_staff(self.company, full_name="Abebe Agent", phone="0911777002", role="AGENT").user
        self.listing = make_listing(company=self.company, units=3, price=Decimal("100000"))
        self.buyer = make_user("Meron Tadesse")
        self.api = client_for(self.buyer)

    def order(self, plan="FULL", **kw):
        return orders.create_order(user=self.buyer, listing=self.listing, plan=plan, **kw)

    def submit(self, order, amount, ref, *, payer="Meron Tadesse", when=None, bank="CBE", register=True, **facts):
        when = when or timezone.now()
        if register:
            FakeVerifier.register(bank, ref, **{"amount": str(amount), "payer": payer,
                                                "receiver_account": order.pay_to_account["account_number"],
                                                "transferred_at": when, "narration": f"pay {order.payment_code}",
                                                **facts})
        return bank_transfer.submit_proof(order, order.user, file=receipt(), bank=bank, reference=ref, amount=amount,
                                          transfer_datetime=when, payer_name=payer)

    def confirm(self, proof, user=None, *, amount=None, payer=None, when=None, ref="ST1"):
        return bank_transfer.confirm_proof(proof, user or self.owner, amount=amount or proof.amount_verified,
                                           payer_name=payer or proof.payer_name,
                                           credited_at=when or proof.transfer_datetime, statement_ref=ref)


# =========================================================================== order set-up & instructions
class InstructionsTests(Base):
    def test_new_order_is_bank_transfer_with_code_and_frozen_account(self):
        o = self.order()
        self.assertEqual(o.payment_method, Order.PaymentMethod.BANK_TRANSFER)
        self.assertTrue(o.payment_code.startswith("P"))
        acc = PartnerBankAccount.objects.get(agency=self.company)
        self.assertEqual(o.pay_to_account["account_number"], acc.account_number)
        # the hold for the first receipt
        self.assertAlmostEqual((o.expires_at - timezone.now()).total_seconds() / 3600, 24, delta=0.1)
        # changing the company's account later doesn't change what this buyer was told
        PartnerBankAccount.objects.filter(pk=acc.pk).update(account_number="1000999999999")
        r = self.api.get(f"/api/market/orders/{o.reference}/bank-instructions/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["account"]["account_number"], acc.account_number)
        self.assertEqual(r.data["payment_code"], o.payment_code)
        self.assertEqual(r.data["amount_due_now"], "100000.00")
        self.assertTrue(any("never send account changes" in line for line in r.data["instructions"]))

    def test_company_without_usable_account_cant_take_orders(self):
        PartnerBankAccount.objects.filter(agency=self.company).update(usable_from=timezone.now() + timedelta(hours=5))
        with self.assertRaises(BusinessRuleError):
            self.order()


# =========================================================================== receipts & references
class ReceiptTests(Base):
    def test_upload_via_api_extends_hold_and_creates_one_pending_transaction(self):
        o = self.order()
        when = timezone.now()
        r = self.api.post(f"/api/market/orders/{o.reference}/proofs/", {
            "file": receipt(), "bank": "CBE", "reference": "ft 2611-1abc de", "amount": "40000",
            "transfer_datetime": when.isoformat(), "payer_name": "Meron Tadesse"}, format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status_label"], "Verifying with bank")
        self.assertIn("/api/files/", r.data["receipt_url"])
        proof = BankTransferProof.objects.get()
        self.assertEqual(proof.normalised_reference, "FT26111ABCDE")
        txn = proof.payment_transaction
        self.assertEqual((txn.provider, txn.status, txn.amount), ("bank", "PENDING", Decimal("40000.00")))
        o.refresh_from_db()
        self.assertGreater(o.expires_at, timezone.now() + timedelta(days=2))  # +3 business days
        self.assertEqual(ListingUnit.objects.get(pk=o.unit_id).held_until, o.expires_at)
        # the signed link works, an altered one doesn't
        from urllib.parse import urlparse

        path = urlparse(r.data["receipt_url"]).path
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get(path[:-3] + "xx/").status_code, 404)

    def test_only_real_images_or_pdfs(self):
        o = self.order()
        bad = SimpleUploadedFile("r.jpg", b"MZ not an image", content_type="image/jpeg")
        with self.assertRaises(BusinessRuleError):
            bank_transfer.submit_proof(o, self.buyer, file=bad, bank="CBE", reference="FT12345678", amount=10,
                                       transfer_datetime=timezone.now(), payer_name="Meron Tadesse")

    def test_duplicate_reference_rejected_across_orders_and_agencies(self):
        o1 = self.order()
        self.submit(o1, Decimal("1000"), "FT11112222AB")
        other_company = make_company(name="Other Motors", kind="CAR_DEALER")
        other_listing = make_listing(company=other_company, units=1, price=Decimal("5000"))
        o2 = orders.create_order(user=make_user("Someone Else"), listing=other_listing, plan="FULL")
        api = client_for(o2.user)
        r = api.post(f"/api/market/orders/{o2.reference}/proofs/", {
            "file": receipt(), "bank": "CBE", "reference": "FT1111-2222ab", "amount": "1000",
            "transfer_datetime": timezone.now().isoformat(), "payer_name": "Someone Else"}, format="multipart")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data["code"], "duplicate_reference")
        self.assertFalse(BankTransferProof.objects.filter(order=o2).exists())
        self.assertTrue(FraudAlert.objects.filter(kind="DUPLICATE_REFERENCE", order_reference=o2.reference).exists())
        # the database refuses it too, whatever the code does
        p1 = BankTransferProof.objects.get(order=o1)
        txn = PaymentTransaction.objects.create(user=o2.user, provider="bank", purpose="ORDER", amount=1, target=o2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BankTransferProof.objects.create(order=o2, payment_transaction=txn, bank="CBE",
                                             normalised_reference=p1.normalised_reference, reference_as_typed="x",
                                             payer_name="x", amount_claimed=1, transfer_datetime=timezone.now())

    def test_rejected_reference_can_be_corrected_and_reused(self):
        o = self.order()
        p = self.submit(o, Decimal("5000"), "FT55556666AB", register=False)  # the bank doesn't know it yet
        p = bank_transfer.verify_proof(p)
        self.assertEqual(p.status, P.REJECTED)
        self.assertFalse(BankReference.objects.filter(reference="FT55556666AB").exists())
        p2 = self.submit(o, Decimal("5000"), "FT55556666AB")
        self.assertEqual(bank_transfer.verify_proof(p2).status, P.VERIFIED_AWAITING_PARTNER)

    def test_payer_must_be_buyer_or_declared_co_payer(self):
        o = self.order()
        with self.assertRaises(BusinessRuleError):
            self.submit(o, Decimal("1000"), "FT90909090AB", payer="Kebede Alemu")
        FakeVerifier.register("CBE", "FT90909090AB", amount="1000", payer="Kebede Alemu",
                              receiver_account=o.pay_to_account["account_number"], transferred_at=timezone.now())
        p = bank_transfer.submit_proof(o, self.buyer, file=receipt(), bank="CBE", reference="FT90909090AB",
                                       amount=1000, transfer_datetime=timezone.now(), payer_name="Kebede Alemu",
                                       declare_co_payer=True)
        self.assertEqual(bank_transfer.verify_proof(p).status, P.VERIFIED_AWAITING_PARTNER)


# =========================================================================== ShegerPay verification
class VerificationTests(Base):
    def check(self, ref, *, claimed=Decimal("1000"), when=None, **facts):
        o = self.order()
        when = when or timezone.now()
        base = {"amount": str(claimed), "payer": "Meron Tadesse",
                "receiver_account": o.pay_to_account["account_number"], "transferred_at": when,
                "narration": f"pay {o.payment_code}"}
        base.update(facts)
        FakeVerifier.register("CBE", ref, **base)
        p = bank_transfer.submit_proof(o, self.buyer, file=receipt(), bank="CBE", reference=ref, amount=claimed,
                                       transfer_datetime=when, payer_name="Meron Tadesse")
        return bank_transfer.verify_proof(p)

    def test_pass(self):
        p = self.check("FT00000001AA")
        self.assertEqual(p.status, P.VERIFIED_AWAITING_PARTNER)
        self.assertEqual(p.amount_verified, Decimal("1000.00"))
        self.assertTrue(p.order.events.filter(kind="PROOF_VERIFIED").exists())

    def test_amount_mismatch(self):
        with self.captureOnCommitCallbacks(execute=True):
            p = self.check("FT00000002AA", amount="999.99")
        self.assertEqual(p.status, P.REJECTED)
        self.assertIn("999.99", p.rejection_reason)
        self.assertEqual(p.payment_transaction.status, "FAILED")
        self.assertTrue(p.order.events.filter(kind="PROOF_REJECTED").exists())
        self.assertTrue(self.buyer.notifications.filter(title="Receipt rejected").exists())

    def test_wrong_receiver(self):
        self.assertIn("seller's account", self.check("FT00000003AA", receiver_account="1000222333444").rejection_reason)

    def test_masked_receiver_that_lines_up_passes(self):
        o_acc = make_company  # noqa: F841 – readability
        p = self.check("FT00000009AA")
        self.assertEqual(p.status, P.VERIFIED_AWAITING_PARTNER)
        self.assertTrue(bank_transfer.accounts_match("1000****" + p.order.pay_to_account["account_number"][-4:],
                                                     p.order.pay_to_account["account_number"]))

    def test_wrong_payer(self):
        self.assertIn("not the buyer", self.check("FT00000004AA", payer="Somebody Different").rejection_reason)

    def test_time_outside_window(self):
        p = self.check("FT00000005AA", transferred_at=timezone.now() - timedelta(hours=3))
        self.assertEqual(p.status, P.REJECTED)

    def test_missing_payment_code_in_narration(self):
        self.assertIn("payment code", self.check("FT00000006AA", narration="house money").rejection_reason)

    def test_bank_returns_another_reference(self):
        p = self.check("FT00000007AA")
        r = ShegerPayVerifier.parse({"verified": True, "status": "verified", "transaction_id": "FT99999999ZZ",
                                     "amount": 1000})
        self.assertEqual(r.outcome, "FOUND")
        problems, _ = bank_transfer._check_facts(p, p.order, r)
        self.assertTrue(any("different transaction reference" in x for x in problems))

    def test_reused_reference_never_reaches_the_bank(self):
        self.check("FT00000008AA")
        with self.assertRaises(DuplicateReference):
            self.check("FT00000008AA")

    def test_outage_stays_verifying_retries_with_backoff_then_escalates(self):
        o = self.order()
        p = self.submit(o, Decimal("1000"), "FT00000010AA")
        FakeVerifier.outage = True
        p = bank_transfer.verify_proof(p)
        self.assertEqual((p.status, p.verify_attempts), (P.VERIFYING, 1))
        first_next = p.next_check_at
        BankTransferProof.objects.filter(pk=p.pk).update(next_check_at=timezone.now())
        p = bank_transfer.verify_proof(BankTransferProof.objects.get(pk=p.pk))
        self.assertEqual((p.status, p.verify_attempts), (P.VERIFYING, 2))
        self.assertGreater(p.next_check_at - timezone.now(), timedelta(minutes=1))  # backoff grows
        self.assertIsNotNone(first_next)
        BankTransferProof.objects.filter(pk=p.pk).update(created_at=timezone.now() - timedelta(hours=7))
        self.assertEqual(bank_transfer.escalate_stuck_verifications(), 1)
        self.assertTrue(FraudAlert.objects.filter(kind="VERIFIER_OUTAGE").exists())
        self.assertEqual(bank_transfer.escalate_stuck_verifications(), 0)  # only once
        FakeVerifier.outage = False
        BankTransferProof.objects.filter(pk=p.pk).update(next_check_at=timezone.now())
        out = bank_transfer.verify_due_proofs()
        self.assertEqual(out["verified"], 1)

    def test_bank_pending_stays_verifying(self):
        p = self.check("FT00000011AA", pending=True)
        self.assertEqual(p.status, P.VERIFYING)

    def test_usd_receipt_goes_to_manual_review(self):
        from apps.core.testutils import make_bank_account

        PartnerBankAccount.objects.filter(agency=self.company).delete()
        make_bank_account(self.company, currency="USD")
        self.listing.currency = "USD"
        self.listing.save()
        o = self.order()
        p = self.submit(o, Decimal("100"), "SWIFT0001234", register=False)
        self.assertTrue(p.requires_manual_review)
        self.assertTrue(FraudAlert.objects.filter(kind="MANUAL_REVIEW").exists())
        self.assertEqual(bank_transfer.verify_due_proofs()["verified"], 0)  # never sent to ShegerPay
        staff = make_user("Nebret Ops", staff=True)
        p = bank_transfer.manual_review(p, staff, approve=True, note="Checked SWIFT MT103 with the bank")
        self.assertEqual(p.status, P.VERIFIED_AWAITING_PARTNER)

    def test_shegerpay_parse(self):
        r = ShegerPayVerifier.parse({"valid": True, "status": "verified", "amount": "1500.00", "payer": "ABEBE KEBEDE",
                                     "transaction_id": "FT1"})
        self.assertEqual((r.outcome, r.amount, r.payer_name), ("FOUND", Decimal("1500.00"), "ABEBE KEBEDE"))
        self.assertEqual(ShegerPayVerifier.parse({"valid": False, "status": "pending"}).outcome, "PENDING")
        self.assertEqual(ShegerPayVerifier.parse({"valid": False, "reason": "not found"}).outcome, "NOT_FOUND")

    @override_settings(NEBRET={**settings.NEBRET, "RECEIPT_VERIFIER": "shegerpay"},
                       SHEGERPAY={"API_KEY": "sk_test_x", "BASE_URL": "https://api.shegerpay.com", "TIMEOUT": 5})
    def test_shegerpay_http_and_strict_receiver_rule(self):
        o = self.order()
        p = self.submit(o, Decimal("1000"), "FT26062K7WMY", register=False)
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"valid": True, "verified": True, "status": "verified", "amount": 1000,
                                  "payer": "Meron Tadesse", "transaction_id": "FT26062K7WMY"}
        with mock.patch("apps.payments.verifiers.requests.post", return_value=resp) as post:
            p = bank_transfer.verify_proof(p)
        self.assertEqual(post.call_args.kwargs["headers"]["X-API-Key"], "sk_test_x")
        self.assertEqual(post.call_args.kwargs["data"]["provider"], "cbe")
        # ShegerPay didn't return the receiving account → strict mode sends it to manual review
        self.assertTrue(p.requires_manual_review)
        self.assertEqual(p.status, P.VERIFYING)
        resp503 = mock.Mock(status_code=503)
        resp503.json.return_value = {"detail": "down"}
        p2 = self.submit(o, Decimal("1000"), "FT26062K7WMZ", register=False)
        with mock.patch("apps.payments.verifiers.requests.post", return_value=resp503):
            p2 = bank_transfer.verify_proof(p2)
        self.assertEqual(p2.status, P.VERIFYING)


# =========================================================================== seller confirmation
class ConfirmationTests(Base):
    def verified(self, order=None, amount=Decimal("100000"), ref="FT77770000AA"):
        order = order or self.order()
        return bank_transfer.verify_proof(self.submit(order, amount, ref))

    def test_confirm_applies_payment_commission_events_agreement(self):
        p = self.verified()
        with self.captureOnCommitCallbacks(execute=True):
            p = self.confirm(p)
        self.assertEqual(p.status, P.CONFIRMED)
        o = Order.objects.get(pk=p.order_id)
        self.assertEqual((o.status, o.amount_paid), (Order.Status.PAID, Decimal("100000.00")))
        self.assertEqual(p.payment_transaction.status, "SUCCESS")
        e = AgencyLedgerEntry.objects.get(order=o)
        self.assertEqual((e.direction, e.payout_status, e.commission),
                         ("TO_NEBRET", "RECEIVABLE", Decimal("5000.00")))
        self.assertTrue(o.events.filter(kind="PARTNER_CONFIRMED").exists())
        self.assertTrue(o.events.filter(kind="AGREEMENT_ISSUED").exists())
        self.assertEqual(OrderAgreement.objects.filter(order=o).count(), 1)

    def test_mismatch_amount_payer_or_time_is_disputed_and_not_applied(self):
        for i, kw in enumerate([{"amount": Decimal("99999")}, {"payer": "Kebede Alemu"},
                                {"when": timezone.now() + timedelta(hours=2)}]):
            o = orders.create_order(user=self.buyer, listing=self.listing, plan="FULL")
            p = self.verified(o, ref=f"FT8888000{i}AA")
            p = self.confirm(p, **kw)
            self.assertEqual(p.status, P.DISPUTED, kw)
            o.refresh_from_db()
            self.assertEqual(o.amount_paid, 0)
            self.assertEqual(p.payment_transaction.status, "PENDING")
            self.assertTrue(FraudAlert.objects.filter(kind="DISPUTE", proof_id=p.pk).exists())
        # Nebret settles a dispute for the seller (note + evidence) – then it is applied
        staff = make_user("Nebret Ops", staff=True)
        with self.assertRaises(BusinessRuleError):
            bank_transfer.confirm_proof(p, staff, amount=p.amount_verified, payer_name=p.payer_name,
                                        credited_at=p.transfer_datetime, statement_ref="X")  # no note / evidence
        p = bank_transfer.confirm_proof(p, staff, amount=p.amount_verified, payer_name=p.payer_name,
                                        credited_at=p.transfer_datetime, statement_ref="X", note="Seller emailed",
                                        evidence=SimpleUploadedFile("e.pdf", b"%PDF-1.4 x", "application/pdf"))
        self.assertEqual(p.status, P.CONFIRMED)

    def test_agent_cant_confirm(self):
        p = self.verified()
        with self.assertRaises(BusinessRuleError):
            self.confirm(p, self.agent)
        api = client_for(self.agent)
        r = api.post(f"/api/company/payments/{p.pk}/", {"action": "confirm", "amount": "100000",
                                                       "payer_name": p.payer_name,
                                                       "credited_at": p.transfer_datetime.isoformat(),
                                                       "statement_ref": "S"}, format="json")
        self.assertEqual(r.status_code, 409)

    @override_settings(NEBRET={**settings.NEBRET, "DUAL_CONTROL_THRESHOLD": "50000"})
    def test_dual_control_above_threshold_needs_two_different_people(self):
        p = self.verified()
        p = self.confirm(p, self.owner)
        self.assertEqual(p.status, P.VERIFIED_AWAITING_PARTNER)
        self.assertTrue(p.needs_second_confirmer)
        self.assertEqual(Order.objects.get(pk=p.order_id).amount_paid, 0)
        with self.assertRaises(BusinessRuleError):
            self.confirm(p, self.owner)  # the same person again
        p = self.confirm(p, self.manager)
        self.assertEqual(p.status, P.CONFIRMED)
        self.assertEqual(p.second_confirmer, self.manager)
        self.assertEqual(Order.objects.get(pk=p.order_id).amount_paid, Decimal("100000.00"))

    def test_below_threshold_one_confirmation_is_enough(self):
        p = self.verified()
        self.assertEqual(self.confirm(p, self.manager).status, P.CONFIRMED)

    def test_double_confirm_runs_fulfilment_once(self):
        p = self.verified()
        self.confirm(p)
        with self.assertRaises(BusinessRuleError):
            self.confirm(BankTransferProof.objects.get(pk=p.pk))
        o = Order.objects.get(pk=p.order_id)
        self.assertEqual(o.amount_paid, Decimal("100000.00"))
        self.assertEqual(o.events.filter(kind="PAID_IN_FULL").count(), 1)
        self.assertEqual(AgencyLedgerEntry.objects.filter(order=o).count(), 1)

    def test_reject_by_seller(self):
        p = self.verified()
        p = bank_transfer.reject_proof(p, self.manager, "Not on our statement")
        self.assertEqual(p.status, P.REJECTED)
        self.assertEqual(Order.objects.get(pk=p.order_id).amount_paid, 0)

    def test_sla_escalation(self):
        p = self.verified()
        self.assertEqual(bank_transfer.escalate_overdue_confirmations(), 0)
        BankTransferProof.objects.filter(pk=p.pk).update(verified_at=timezone.now() - timedelta(days=6))
        self.assertEqual(bank_transfer.escalate_overdue_confirmations(), 1)
        self.assertTrue(FraudAlert.objects.filter(kind="PARTNER_SLA").exists())
        self.assertTrue(self.owner.notifications.filter(title="Payment waiting for your confirmation").exists())


# =========================================================================== split transfers & installments
class SplitTransferTests(Base):
    def test_order_confirmed_only_when_confirmed_total_reaches_first_payment(self):
        o = self.order(plan="INSTALLMENT", months=4, down_payment=Decimal("40000"))
        self.assertEqual(o.first_payment, Decimal("40000"))
        bank_pay(o, Decimal("15000"), confirmer=self.owner)
        o.refresh_from_db()
        self.assertEqual((o.status, o.amount_paid), (Order.Status.PENDING_PAYMENT, Decimal("15000")))
        # a receipt that is only verified (not confirmed) doesn't count
        p = bank_transfer.verify_proof(self.submit(o, Decimal("25000"), "FT33330000AA"))
        o.refresh_from_db()
        self.assertEqual(o.status, Order.Status.PENDING_PAYMENT)
        self.confirm(p)
        o.refresh_from_db()
        self.assertEqual(o.status, Order.Status.INSTALLMENTS)
        self.assertEqual(o.installments.count(), 4)
        # 15,000 + 15,000 each month: a 20,000 transfer pays #1 and half of #2 – oldest first
        bank_pay(o, Decimal("20000"), confirmer=self.owner)
        inst = list(o.installments.order_by("number"))
        self.assertEqual(inst[0].status, "PAID")
        self.assertEqual(inst[1].paid_amount, Decimal("5000"))
        self.assertEqual(inst[2].paid_amount, 0)

    def test_claim_cant_exceed_balance_including_receipts_under_review(self):
        o = self.order()
        self.submit(o, Decimal("60000"), "FT44440000AA")
        with self.assertRaises(BusinessRuleError):
            self.submit(o, Decimal("50000"), "FT44440001AA")


# =========================================================================== holds
class HoldTests(Base):
    def test_unit_not_released_while_receipt_in_review(self):
        o = self.order()
        self.submit(o, Decimal("1000"), "FT12121212AA")
        Order.objects.filter(pk=o.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        ListingUnit.objects.filter(pk=o.unit_id).update(held_until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(orders.expire_unpaid_orders(), 0)
        self.assertEqual(ListingUnit.objects.get(pk=o.unit_id).status, ListingUnit.Status.HELD)

    def test_unit_not_released_while_money_paid(self):
        o = self.order(plan="INSTALLMENT", months=4, down_payment=Decimal("40000"))
        bank_pay(o, Decimal("1000"), confirmer=self.owner)
        Order.objects.filter(pk=o.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        ListingUnit.objects.filter(pk=o.unit_id).update(held_until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(orders.expire_unpaid_orders(), 0)
        self.assertEqual(ListingUnit.objects.get(pk=o.unit_id).status, ListingUnit.Status.HELD)

    def test_cancel_expires_open_receipts_and_sets_refund_deadline(self):
        o = self.order(plan="INSTALLMENT", months=4, down_payment=Decimal("40000"))
        bank_pay(o, Decimal("1000"), confirmer=self.owner)
        p = self.submit(o, Decimal("2000"), "FT65650000AA")
        staff = make_user("Nebret Ops", staff=True)
        orders.cancel_order(o, "Buyer asked", staff=staff)
        p.refresh_from_db()
        o.refresh_from_db()
        self.assertEqual(p.status, P.EXPIRED)
        self.assertEqual(o.refund_due_by, timezone.localdate() + timedelta(days=14))
        Order.objects.filter(pk=o.pk).update(refund_due_by=timezone.localdate() - timedelta(days=1))
        self.assertEqual(bank_transfer.escalate_overdue_refunds(), 1)
        bank_transfer.record_partner_refund(o, self.owner, amount=Decimal("1000"), reference="FT-REFUND-1",
                                            refunded_on=timezone.localdate(),
                                            evidence=SimpleUploadedFile("r.pdf", b"%PDF-1.4", "application/pdf"))
        o.refresh_from_db()
        self.assertEqual(o.status, Order.Status.REFUNDED)


# =========================================================================== commission & gateway flows
class GatewayUnchangedTests(TestCase):
    def test_gateway_amount_rule_still_applies_and_equb_uses_gateway(self):
        from apps.payments.services import apply_status, initiate_payment

        g = make_equb(name="Car Equb", contribution_amount=Decimal("5000"), max_members=3,
                      start_date=timezone.localdate(), duration_value=3, duration_unit="MONTHS")
        from apps.equb import services as equb

        user = make_user("Member")
        m = equb.join_group(user, g, auto_start=False)
        equb.start_group(g)
        c = m.contributions.order_by("round__number").first()
        txn = initiate_payment(user=user, target=c, amount=Decimal("5000"), purpose="EQUB_CONTRIBUTION",
                               provider="mock")
        txn = apply_status(txn.pk, "SUCCESS", amount=Decimal("4000"))
        self.assertEqual(txn.status, "FAILED")  # paying less through a gateway still fails
        self.assertIn("Amount mismatch", txn.failure_reason)


# =========================================================================== EQUB_PAYOUT removal
class EqubPayoutRemovalTests(Base):
    def test_new_orders_cant_use_equb_payout_and_old_ones_still_load(self):
        r = self.api.post("/api/market/orders/", {"listing": self.listing.slug, "plan": "EQUB_PAYOUT"}, format="json")
        self.assertEqual(r.status_code, 409)
        r = self.api.post(f"/api/market/listings/{self.listing.slug}/quote/", {"plan": "EQUB_PAYOUT"}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("EQUB_PAYOUT", self.api.get(f"/api/market/listings/{self.listing.slug}/").data["payment_plans"])
        o = self.order()
        Order.objects.filter(pk=o.pk).update(plan="EQUB_PAYOUT", payment_method="", payment_code=None,
                                             pay_to_account={}, funding_source="EQUB_PAYOUT")
        r = self.api.get(f"/api/market/orders/{o.reference}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["plan"], "EQUB_PAYOUT")
        self.assertIsNone(r.data["pay_to_account"])


# =========================================================================== product Equbs
class ProductEqubTests(Base):
    def test_listing_shows_only_open_groups_with_spots(self):
        from apps.equb import product
        from apps.equb.models import EqubGroup

        staff = make_user("Nebret Ops", staff=True)
        with self.assertRaises(BusinessRuleError):  # payout below the price
            product.create_product_equb(self.listing, staff, contribution_amount=1000, duration_value=10,
                                        members=2, start_date=timezone.localdate())
        g, maths = product.create_product_equb(self.listing, staff, contribution_amount=2000, duration_value=60,
                                               duration_unit="DAYS", members=2, start_date=timezone.localdate())
        self.assertTrue(maths["covers_price"])
        self.assertEqual(ListingUnit.objects.filter(equb_group=g, status="EQUB").count(), 2)
        rows = self.api.get(f"/api/market/listings/{self.listing.slug}/").data["equb_groups"]
        self.assertEqual([r["slug"] for r in rows], [g.slug])
        self.assertEqual(rows[0]["spots_left"], 2)
        EqubGroup.objects.filter(pk=g.pk).update(max_members=0)  # no places left
        self.assertEqual(self.api.get(f"/api/market/listings/{self.listing.slug}/").data["equb_groups"], [])
        EqubGroup.objects.filter(pk=g.pk).update(status="COMPLETED")  # finished – nobody can join any more
        self.assertEqual(self.api.get(f"/api/market/listings/{self.listing.slug}/").data["equb_groups"], [])
        with self.assertRaises(BusinessRuleError):  # agents can't create product Equbs
            product.create_product_equb(self.listing, self.agent, contribution_amount=2000, duration_value=60,
                                        members=1, start_date=timezone.localdate())

    def test_win_creates_funded_order_with_reserved_unit(self):
        from apps.equb import product
        from apps.equb import services as equb

        g, _ = product.create_product_equb(self.listing, self.owner, contribution_amount=2000, duration_value=60,
                                           duration_unit="DAYS", members=2, start_date=timezone.localdate(),
                                           draw_type="MANUAL")
        winner = make_user("Selam Winner")
        m1 = equb.join_group(winner, g, auto_start=False)
        m2 = equb.join_group(make_user("Other Member"), g, auto_start=False)
        equb.start_group(g)
        staff = make_user("Nebret Ops", staff=True)
        for m in (m1, m2):
            equb.record_office_payment(m, staff, method="cash", rounds=1)
        r1 = g.rounds.get(number=1)
        with plenty_of_money(), self.captureOnCommitCallbacks(execute=True):
            payouts = equb.run_draw(r1, staff, winner_ids=[m1.pk])
        payout = payouts[0]
        payout.refresh_from_db()
        self.assertEqual(payout.status, "REDEEMED")
        o = payout.order
        self.assertEqual((o.user, o.plan, o.funding_source, o.status),
                         (winner, "EQUB_PAYOUT", "EQUB_PAYOUT", Order.Status.PAID))
        self.assertEqual(o.unit.equb_group, g)
        self.assertEqual(o.unit.status, ListingUnit.Status.SOLD)
        self.assertEqual(o.unit_price, Decimal("100000.00"))  # locked price
        e = AgencyLedgerEntry.objects.get(order=o)
        self.assertEqual((e.kind, e.payout_status), ("CREDIT", "OWED"))
        self.assertTrue(winner.notifications.filter(kind="EQUB_WIN", title__contains="won").exists())


# =========================================================================== agreement
class AgreementTests(Base):
    def test_qr_verifies_and_tampered_signature_fails_and_versions(self):
        o = self.order(plan="INSTALLMENT", months=4, down_payment=Decimal("40000"))
        with self.captureOnCommitCallbacks(execute=True):
            bank_pay(o, Decimal("40000"), confirmer=self.owner)
        a = OrderAgreement.objects.get(order=o)
        self.assertTrue(agreement.verify_url(a).endswith(f"/agreements/verify/{o.reference}/1/{a.signature}/"))
        from apps.core import private_files

        self.assertTrue(private_files.read(a.file).startswith(b"%PDF"))
        r = self.client.get(f"/agreements/verify/{o.reference}/1/{a.signature}/")
        self.assertContains(r, "Genuine agreement")
        self.assertContains(r, self.company.name)
        self.assertNotContains(r, self.buyer.full_name)  # only ref, company and date are public
        bad = a.signature[:-1] + ("A" if a.signature[-1] != "A" else "B")
        self.assertEqual(self.client.get(f"/agreements/verify/{o.reference}/1/{bad}/").status_code, 404)
        with self.captureOnCommitCallbacks(execute=True):
            bank_pay(o, Decimal("15000"), confirmer=self.owner)
        self.assertEqual(OrderAgreement.objects.filter(order=o).count(), 2)
        self.assertIsNotNone(OrderAgreement.objects.get(order=o, version=1).superseded_at)
        rows = self.api.get(f"/api/market/orders/{o.reference}/agreement/").data
        self.assertEqual([r["version"] for r in rows], [2, 1])
        self.assertTrue(rows[0]["download_url"])


# =========================================================================== business days
class BusinessDayTests(TestCase):
    @override_settings(NEBRET={**settings.NEBRET, "HOLIDAYS": ["2026-11-18"]})
    def test_weekends_and_holidays_are_skipped(self):
        tz = timezone.get_current_timezone()
        mon = datetime(2026, 11, 16, 10, 0, tzinfo=tz)
        self.assertEqual(add_business_days(mon, 2).date().isoformat(), "2026-11-19")  # Wed is a holiday
        fri = datetime(2026, 11, 20, 10, 0, tzinfo=tz)
        self.assertEqual(add_business_days(fri, 1).date().isoformat(), "2026-11-23")  # → Monday
        self.assertEqual(business_days_between(fri, datetime(2026, 11, 23, 9, 0, tzinfo=tz)), 0)
        self.assertEqual(business_days_between(fri, datetime(2026, 11, 23, 11, 0, tzinfo=tz)), 1)

    @override_settings(NEBRET={**settings.NEBRET, "HOLIDAYS": ["2026-11-23", "2026-11-24"]})
    def test_sla_waits_over_holidays(self):
        t = TestCase  # noqa: F841
        tz = timezone.get_current_timezone()
        fri = datetime(2026, 11, 20, 10, 0, tzinfo=tz)
        self.assertEqual(business_days_between(fri, datetime(2026, 11, 25, 9, 0, tzinfo=tz)), 0)
        self.assertEqual(business_days_between(fri, datetime(2026, 11, 26, 11, 0, tzinfo=tz)), 2)


# =========================================================================== bank accounts
class BankAccountTests(TestCase):
    def test_owner_submits_staff_approves_cooling_off_and_publish_gate(self):
        from apps.agencies import bank_accounts as ba

        company = make_company(bank_account=False)
        owner = company_owner(company)
        manager = add_staff(company, full_name="Mimi Manager", phone="0911777003", role="MANAGER").user
        listing = make_listing(company=company, units=1)
        api = client_for(owner)
        r = client_for(manager).post("/api/company/bank-accounts/", {"bank": "CBE", "account_name": "Sunshine",
                                                                     "account_number": "1000123456789"}, format="json")
        self.assertEqual(r.status_code, 409)  # only the owner
        r = api.post("/api/company/bank-accounts/", {"bank": "CBE", "account_name": "Sunshine RE",
                                                     "account_number": "1000 1234 56789"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        acc = PartnerBankAccount.objects.get(pk=r.data["id"])
        self.assertFalse(ba.has_usable_account(company))
        r = api.post(f"/api/company/listings/{listing.pk}/", {"action": "publish"}, format="json")
        self.assertEqual(r.status_code, 409)
        staff = make_user("Nebret Ops", staff=True)
        ba.approve_account(acc, staff, "Called the bank")
        self.assertFalse(ba.has_usable_account(company))  # cooling-off
        PartnerBankAccount.objects.filter(pk=acc.pk).update(usable_from=timezone.now() - timedelta(minutes=1))
        self.assertTrue(ba.has_usable_account(company))
        self.assertEqual(api.post(f"/api/company/listings/{listing.pk}/", {"action": "publish"},
                                  format="json").status_code, 200)
        self.assertGreaterEqual(acc.events.count(), 2)
        self.assertTrue(owner.notifications.filter(title="Bank account approved").exists())
        # a USD account needs a SWIFT code
        r = api.post("/api/company/bank-accounts/", {"bank": "DASHEN", "account_name": "Sunshine RE",
                                                     "account_number": "5000123456", "currency": "USD"}, format="json")
        self.assertEqual(r.status_code, 409)
        # without a usable account, live listings are paused by the scheduler
        ba.retire_account(acc, owner, "closing")
        self.assertEqual(ba.pause_listings_without_account(), 1)


# =========================================================================== concurrency
class ConcurrencyTests(TransactionTestCase):
    def test_concurrent_confirmations_on_one_order(self):
        FakeVerifier.reset()
        company = make_company()
        owner = company_owner(company)
        manager = add_staff(company, full_name="Mimi Manager", phone="0911777004", role="MANAGER").user
        listing = make_listing(company=company, units=1, price=Decimal("100000"))
        buyer = make_user("Meron Tadesse")
        o = orders.create_order(user=buyer, listing=listing, plan="FULL")
        proofs = []
        for i in range(2):
            ref, when = f"FT5050000{i}AA", timezone.now()
            FakeVerifier.register("CBE", ref, amount="50000", payer="Meron Tadesse",
                                  receiver_account=o.pay_to_account["account_number"], transferred_at=when)
            p = bank_transfer.submit_proof(o, buyer, file=receipt(), bank="CBE", reference=ref, amount=50000,
                                           transfer_datetime=when, payer_name="Meron Tadesse")
            proofs.append(bank_transfer.verify_proof(p))
        errors, barrier = [], threading.Barrier(3)

        def run(proof, user):
            try:
                barrier.wait()
                bank_transfer.confirm_proof(proof, user, amount=Decimal("50000"), payer_name="Meron Tadesse",
                                            credited_at=proof.transfer_datetime, statement_ref="S")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connection.close()

        # two confirmations of two receipts, plus a duplicate confirmation of the first one
        threads = [threading.Thread(target=run, args=(proofs[0], owner)),
                   threading.Thread(target=run, args=(proofs[1], manager)),
                   threading.Thread(target=run, args=(proofs[0], manager))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        o.refresh_from_db()
        self.assertEqual(o.amount_paid, Decimal("100000.00"))
        self.assertEqual(o.status, Order.Status.PAID)
        self.assertEqual(o.events.filter(kind="PAID_IN_FULL").count(), 1)
        self.assertEqual(len(errors), 1)  # the duplicate confirmation was refused
        self.assertEqual(AgencyLedgerEntry.objects.filter(order=o).count(), 2)


class PagesTests(Base):
    def test_console_and_portal_pages_with_receipts(self):
        o = self.order()
        p = bank_transfer.verify_proof(self.submit(o, Decimal("50000"), "FT10101010AA"))
        staff = make_user("Nebret Ops", staff=True)
        self.client.force_login(staff)
        from django.urls import reverse

        cid = client_for(self.buyer).post("/api/chat/conversations/", {"listing": self.listing.slug},
                                          format="json").data["id"]
        for url in [reverse("console:proofs"), reverse("console:proofs") + "?status=manual",
                    reverse("console:proof", args=[p.pk]), reverse("console:order", args=[o.reference]),
                    reverse("console:alerts"), reverse("console:bank_accounts"), reverse("console:commission"),
                    reverse("console:conversations") + "?tab=all", reverse("console:conversation", args=[cid])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.post(reverse("console:listing_equb", args=[self.listing.pk]),
                             {"action": "preview", "contribution_amount": "2000", "duration_value": "60",
                              "members": "2", "duration_unit": "DAYS"})
        self.assertContains(r, "per winner")
        self.client.force_login(self.owner)
        r = self.client.get(reverse("agency:order", args=[o.reference]))
        self.assertContains(r, "FT10101010AA")
        self.assertNotContains(r, self.buyer.phone)
        r = self.client.post(reverse("agency:order", args=[o.reference]), {
            "action": "confirm_proof", "proof": p.pk, "amount": "50000", "payer_name": p.payer_name,
            "credited_at": timezone.localtime(p.transfer_datetime).strftime("%Y-%m-%dT%H:%M"),
            "statement_ref": "S1"})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.status, P.CONFIRMED)
