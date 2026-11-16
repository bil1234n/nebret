"""
App Store fixes:
* iPhone donations are completed on the website (/give/<slug>/) in Safari – guideline 3.2.2(iv).
* Giveaways: Nebret is the sponsor and the official rules (with the Apple statement) are in the API – guideline 5.3.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core import signing
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.testutils import client_for, make_user
from apps.payments.models import PaymentTransaction

from .models import CrowdfundingCampaign, Donation, Giveaway
from .rules import APPLE_STATEMENT
from .web import SALT, make_token


@override_settings(ALLOWED_HOSTS=["*"])
class WebDonationTests(TestCase):
    def setUp(self):
        self.user = make_user("Hanna Tesfaye")
        self.campaign = CrowdfundingCampaign.objects.create(
            title="A home for the Alemu family", beneficiary_name="Alemu family", goal_amount=Decimal("500000"),
            platform_fee_percent=Decimal("5"), min_donation=10)

    def link(self):
        res = client_for(self.user).post(f"/api/community/crowdfunding/{self.campaign.slug}/web-link/")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["url"]

    def test_app_gets_a_signed_link_to_the_website(self):
        url = self.link()
        self.assertIn(f"/give/{self.campaign.slug}/?t=", url)

    def test_link_needs_sign_in(self):
        from rest_framework.test import APIClient

        res = APIClient().post(f"/api/community/crowdfunding/{self.campaign.slug}/web-link/")
        self.assertEqual(res.status_code, 401)

    def test_page_shows_the_form_for_a_valid_link(self):
        path = self.link().split("testserver", 1)[1]
        res = self.client.get(path)
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Continue to secure payment")
        self.assertContains(res, "Hanna Tesfaye")

    def test_bad_expired_or_other_campaign_tokens_are_refused(self):
        self.assertEqual(self.client.get(f"/give/{self.campaign.slug}/?t=nonsense").status_code, 403)
        other = CrowdfundingCampaign.objects.create(title="Other", beneficiary_name="X", goal_amount=Decimal("100"))
        self.assertEqual(self.client.get(f"/give/{self.campaign.slug}/?t={make_token(self.user, other)}").status_code, 403)
        old = signing.dumps({"u": self.user.pk, "c": self.campaign.pk}, salt=SALT, compress=True)
        with mock.patch("django.core.signing.time.time", return_value=timezone.now().timestamp() + 31 * 60):
            self.assertEqual(self.client.get(f"/give/{self.campaign.slug}/?t={old}").status_code, 403)

    def test_suspended_account_cannot_use_an_old_link(self):
        token = make_token(self.user, self.campaign)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(f"/give/{self.campaign.slug}/?t={token}").status_code, 403)

    @override_settings(NEBRET={**__import__("django.conf").conf.settings.NEBRET, "WEB_DONATION_PROVIDER": "mock",
                               "ENABLE_MOCK_GATEWAY": True})
    def test_giving_creates_the_donation_for_the_account_and_opens_checkout(self):
        token = make_token(self.user, self.campaign)
        res = self.client.post(f"/give/{self.campaign.slug}/", {"t": token, "amount": 200, "message": "Stay strong",
                                                                "is_anonymous": "on"})
        self.assertEqual(res.status_code, 302, res.content[:500])
        d = Donation.objects.get()
        self.assertEqual((d.user, d.amount, d.fee_amount, d.total_amount, d.is_anonymous, d.message),
                         (self.user, Decimal("200"), Decimal("10.00"), Decimal("210.00"), True, "Stay strong"))
        txn = PaymentTransaction.objects.get()
        self.assertEqual(res["Location"], txn.checkout_url)

    def test_below_minimum_shows_an_error(self):
        token = make_token(self.user, self.campaign)
        res = self.client.post(f"/give/{self.campaign.slug}/", {"t": token, "amount": 5})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(Donation.objects.exists())

    def test_closed_campaign(self):
        self.campaign.status = CrowdfundingCampaign.Status.FUNDED
        self.campaign.save()
        res = self.client.get(f"/give/{self.campaign.slug}/?t={make_token(self.user, self.campaign)}")
        self.assertContains(res, "not accepting donations")

    @override_settings(NEBRET={**__import__("django.conf").conf.settings.NEBRET, "WEB_DONATION_PROVIDER": "mock",
                               "ENABLE_MOCK_GATEWAY": True})
    def test_return_page_is_html_in_a_browser_and_json_for_the_app(self):
        token = make_token(self.user, self.campaign)
        self.client.post(f"/give/{self.campaign.slug}/", {"t": token, "amount": 100})
        txn = PaymentTransaction.objects.get()
        html = self.client.get(f"/api/payments/return/{txn.reference}/", HTTP_ACCEPT="text/html,application/xhtml+xml")
        self.assertEqual(html.status_code, 200)
        self.assertContains(html, "Back to the Nebret Equb app")
        api = self.client.get(f"/api/payments/return/{txn.reference}/", HTTP_ACCEPT="application/json")
        self.assertEqual(api.json()["reference"], str(txn.reference))


class GiveawayRulesTests(TestCase):
    def make(self, **kw):
        now = timezone.now()
        defaults = dict(title="Win an e-bike", prize_type="PURCHASE_CREDIT", prize_title="Electric delivery bike",
                        prize_value=Decimal("85000"), starts_at=now, ends_at=now + timedelta(days=10),
                        draw_at=now + timedelta(days=10, hours=1))
        defaults.update(kw)
        return Giveaway.objects.create(**defaults)

    def test_api_has_sponsor_apple_statement_and_full_rules(self):
        g = self.make(rules="Only one prize per household.")
        data = client_for(make_user()).get(f"/api/community/giveaways/{g.slug}/").json()
        self.assertIn("NIYA FINANCIAL TECHNOLOGIES PLC", data["organizer_name"])
        self.assertEqual(data["apple_statement"], APPLE_STATEMENT)
        self.assertEqual(data["prize_provider"], "")
        titles = [r["title"] for r in data["official_rules"]]
        for t in ("Sponsor", "Apple is not involved", "No purchase necessary", "Who can enter", "How to enter", "Prize",
                  "How the winners are chosen", "Additional rules for this giveaway"):
            self.assertIn(t, titles)
        text = " ".join(r["body"] for r in data["official_rules"])
        self.assertIn("Apple Inc. is not a sponsor", text)
        self.assertIn("Only one prize per household.", text)
        self.assertIn("85,000.00 ETB", text)

    def test_partner_is_shown_as_prize_provider_not_sponsor(self):
        g = self.make(sponsor_type="CORPORATE", sponsor_name="Awash Bank")
        data = client_for(make_user()).get(f"/api/community/giveaways/{g.slug}/").json()
        self.assertEqual(data["prize_provider"], "Awash Bank")
        sponsor = data["official_rules"][0]["body"]
        self.assertIn("sponsored by NIYA FINANCIAL TECHNOLOGIES PLC", sponsor)
        self.assertIn("provided by Awash Bank", sponsor)
