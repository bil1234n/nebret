"""Console "New partner company": the bank account entered is the one buyers pay into; commission goes to Nebret."""
from django.test import TestCase, override_settings
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from apps.agencies.bank_accounts import account_for_order
from apps.agencies.models import Agency, PartnerBankAccount
from apps.core.testutils import make_company, make_user

A = PartnerBankAccount.Status


def _post(**over):
    data = {"name": "Abay Homes", "kind": "REAL_ESTATE", "phone": "+251911223344", "city": "Addis Ababa",
            "commission_percent": "5", "status": "ACTIVE", "owner_name": "Abay Owner", "owner_phone": "+251911556677",
            "owner_password": "583194", "pay_bank": "CBE", "pay_account_name": "Abay Homes PLC",
            "pay_account_number": "1000 2233 4455", "pay_currency": "ETB"}
    data.update(over)
    return data


class NewCompanyBankAccountTests(TestCase):
    def setUp(self):
        self.staff = make_user("Admin", staff=True)
        self.client.force_login(self.staff)

    def test_create_saves_an_approved_account_buyers_can_pay_into_at_once(self):
        r = self.client.post(reverse("console:agency_add"), _post())
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        agency = Agency.objects.get(name="Abay Homes")
        acc = agency.bank_accounts.get()
        self.assertEqual((acc.status, acc.bank, acc.account_number), (A.APPROVED, "CBE", "100022334455"))
        self.assertEqual(acc.verified_by, self.staff)
        self.assertLessEqual(acc.usable_from, timezone.now())  # first account: no cooling-off
        self.assertEqual(account_for_order(agency), acc)
        self.assertEqual(list(acc.events.values_list("action", flat=True).order_by("id")), ["SUBMITTED", "APPROVED", "USABLE_NOW"])

    def test_bank_account_is_required_and_validated_on_create(self):
        r = self.client.post(reverse("console:agency_add"), _post(pay_bank="", pay_account_number=""))
        self.assertEqual(r.status_code, 200)
        self.assertIn("pay_bank", r.context["form"].errors)
        r = self.client.post(reverse("console:agency_add"), _post(pay_account_number="123"))
        self.assertIn("pay_account_number", r.context["form"].errors)
        r = self.client.post(reverse("console:agency_add"), _post(pay_currency="USD"))
        self.assertIn("pay_swift", r.context["form"].errors)
        self.assertFalse(Agency.objects.filter(name="Abay Homes").exists())

    def test_edit_by_staff_replaces_the_account_at_once(self):
        agency = make_company()
        old = agency.bank_accounts.get()
        data = _post(name=agency.name, pay_account_number="1000999988887")
        for n in ("owner_name", "owner_phone", "owner_password"):
            data.pop(n)
        r = self.client.post(reverse("console:agency_edit", args=[agency.pk]), data)
        self.assertEqual(r.status_code, 302)
        new = agency.bank_accounts.exclude(pk=old.pk).get()
        self.assertEqual((new.status, new.replaces_id), (A.APPROVED, old.pk))
        self.assertLessEqual(new.usable_from, timezone.now())  # Nebret entered it itself – no waiting period
        self.assertEqual(account_for_order(agency), new)
        old.refresh_from_db()
        self.assertEqual(old.status, A.RETIRED)

    def test_edit_without_bank_fields_keeps_the_account(self):
        agency = make_company()
        data = _post(name=agency.name, pay_bank="", pay_account_name="", pay_account_number="")
        for n in ("owner_name", "owner_phone", "owner_password"):
            data.pop(n)
        self.assertEqual(self.client.post(reverse("console:agency_edit", args=[agency.pk]), data).status_code, 302)
        self.assertEqual(agency.bank_accounts.count(), 1)

    def test_pages_show_the_commission_account(self):
        nebret = {"bank": "CBE", "account_name": "Nebret Commission", "account_number": "1000777766665",
                  "currency": "ETB", "branch": "", "swift_code": ""}
        with override_settings(NEBRET={**settings.NEBRET, "COMMISSION_BANK_ACCOUNT": nebret}):
            self.assertContains(self.client.get(reverse("console:agency_add")), "1000777766665")
            agency = make_company()
            page = self.client.get(reverse("console:agency", args=[agency.pk]))
            self.assertContains(page, "1000777766665")
            self.assertContains(page, agency.bank_accounts.get().account_number)
