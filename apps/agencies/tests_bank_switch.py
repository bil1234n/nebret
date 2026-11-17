"""Changing a company's bank account never leaves buyers with nowhere to pay."""
from django.test import TestCase
from django.utils import timezone

from apps.agencies.bank_accounts import account_for_order, approve_account, retire_account, submit_account
from apps.agencies.models import PartnerBankAccount
from apps.core.exceptions import BusinessRuleError
from apps.core.testutils import company_owner, make_company, make_user

A = PartnerBankAccount.Status


class BankSwitchTests(TestCase):
    def setUp(self):
        self.co = make_company()
        self.owner = company_owner(self.co)
        self.staff = make_user("Ops", staff=True)
        self.old = self.co.bank_accounts.get()
        self.new = submit_account(self.co, self.owner, bank="DASHEN", account_name="Moenco Motors PLC",
                                  account_number="2943811057511", replaces=self.old)
        approve_account(self.new, self.staff)
        self.new.refresh_from_db()

    def test_waiting_replacement_and_old_one_still_used(self):
        self.assertFalse(self.new.is_usable)
        self.assertEqual(account_for_order(self.co), self.old)

    def test_staff_switching_off_the_old_one_puts_the_new_one_in_use(self):
        retire_account(self.old, self.staff, "changed bank")
        self.new.refresh_from_db()
        self.assertTrue(self.new.is_usable)
        self.assertEqual(account_for_order(self.co), self.new)

    def test_owner_cant_switch_off_the_last_usable_account(self):
        with self.assertRaises(BusinessRuleError):
            retire_account(self.old, self.owner)
        self.assertEqual(account_for_order(self.co), self.old)

    def test_use_now_from_the_console(self):
        self.client.force_login(self.staff)
        self.client.post("/console/bank-accounts/", {"account": self.new.pk, "action": "use_now", "note": "called owner"})
        self.new.refresh_from_db()
        self.old.refresh_from_db()
        self.assertTrue(self.new.is_usable)
        self.assertEqual(self.old.status, A.RETIRED)
        self.assertEqual(account_for_order(self.co), self.new)

    def test_account_entered_by_staff_is_used_at_once(self):
        from apps.agencies.bank_accounts import add_account_by_staff

        acc = add_account_by_staff(self.co, self.staff, bank="BOA", account_name="Moenco Motors PLC",
                                   account_number="55667788990")
        self.assertTrue(acc.is_usable)
        self.assertLessEqual(acc.usable_from, timezone.now())
        self.assertEqual(account_for_order(self.co), acc)
