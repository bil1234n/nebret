"""Content people type is shown in the app's language, translated once and cached."""
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from apps.core.models import Translation
from apps.core.testutils import client_for, make_company, make_listing, make_user


def fake_google(texts, lang):
    return [f"[{lang}] {t}" for t in texts]


@override_settings(GOOGLE_TRANSLATE_API_KEY="k")
class TranslationTests(TestCase):
    def setUp(self):
        self.listing = make_listing(company=make_company(), units=1, price=Decimal("1000"))
        self.listing.features = ["Power steering"]
        self.listing.save()
        self.api = client_for(make_user("Buyer"))

    def test_product_is_shown_in_afaan_oromoo_and_cached(self):
        with mock.patch("apps.core.translation._google", side_effect=fake_google) as g:
            r = self.api.get(f"/api/market/listings/{self.listing.slug}/", HTTP_ACCEPT_LANGUAGE="om")
            self.assertEqual(r.data["title"], f"[om] {self.listing.title}")
            self.assertEqual(r.data["features"], ["[om] Power steering"])
            calls = g.call_count
            r2 = self.api.get(f"/api/market/listings/{self.listing.slug}/", HTTP_ACCEPT_LANGUAGE="om")
            self.assertEqual(r2.data["title"], f"[om] {self.listing.title}")
            self.assertEqual(g.call_count, calls)  # second time from the cache
        self.assertTrue(Translation.objects.filter(lang="om").exists())

    def test_list_pages_translate_too_and_failures_show_the_original(self):
        with mock.patch("apps.core.translation._google", side_effect=fake_google):
            r = self.api.get("/api/market/listings/", HTTP_ACCEPT_LANGUAGE="am")
        rows = r.data["results"] if isinstance(r.data, dict) else r.data
        self.assertTrue(all(row["title"].startswith("[am] ") for row in rows))
        Translation.objects.all().delete()
        with mock.patch("apps.core.translation._google", side_effect=RuntimeError("down")):
            r = self.api.get(f"/api/market/listings/{self.listing.slug}/", HTTP_ACCEPT_LANGUAGE="ar")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["title"], self.listing.title)

    def test_no_key_means_no_translation(self):
        with override_settings(GOOGLE_TRANSLATE_API_KEY="", ANTHROPIC_API_KEY=""):
            r = self.api.get(f"/api/market/listings/{self.listing.slug}/", HTTP_ACCEPT_LANGUAGE="om")
        self.assertEqual(r.data["title"], self.listing.title)
