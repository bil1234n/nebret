"""Product Equb console form: service fee input, "Show the maths" result and suggestions; company listing photos."""
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.agencies.services import add_staff
from apps.core.exceptions import BusinessRuleError
from apps.core.testutils import client_for, make_company, make_listing, make_user
from apps.equb import product


class SuggestionTests(TestCase):
    def test_each_suggestion_covers_the_price_and_is_as_close_as_cents_allow(self):
        price = Decimal("6900000")
        rows = product.suggestions(price=price, contribution=1000000, duration_value=7, fee_percent=2)
        titles = [r["title"] for r in rows]
        self.assertTrue(any("1,005,830.91" in t for t in titles), titles)   # contribution for 7 payments
        self.assertTrue(any(t.startswith("8 payments") for t in titles), titles)
        self.assertTrue(any("1.42%" in t for t in titles), titles)          # fee that fits 1,000,000 × 7
        for r in rows:
            self.assertGreaterEqual(Decimal(r["payout"]), price)
        contribution = next(r for r in rows if "1,005,830.91" in r["title"])
        self.assertLess(Decimal(contribution["difference"]), Decimal("1"))

    def test_exact_match_is_flagged(self):
        m = product.maths(price=980, contribution=100, duration_value=10, fee_percent=2)
        self.assertTrue(m["exact"])
        self.assertEqual(m["gap"], "0.00")


class ProductEqubFormTests(TestCase):
    def setUp(self):
        self.staff = make_user("Ops", staff=True)
        self.client.force_login(self.staff)
        self.listing = make_listing(price=Decimal("6900000"), units=3, company=make_company())
        self.url = reverse("console:listing_equb", args=[self.listing.pk])

    def test_show_the_maths_lists_suggestions_with_use_this(self):
        r = self.client.post(self.url, {"action": "preview", "contribution_amount": "1000000", "duration_value": "7",
                                        "service_fee_percent": "2"})
        self.assertContains(r, "payout is below the product price")
        self.assertContains(r, "Use this", count=4)
        self.assertContains(r, "1,005,830.91")

    def test_custom_service_fee_is_used_and_saved(self):
        r = self.client.post(self.url, {"action": "preview", "contribution_amount": "1000000", "duration_value": "7",
                                        "service_fee_percent": "1.42"})
        self.assertContains(r, "covers the product price")
        r = self.client.post(self.url, {"action": "create", "contribution_amount": "1000000", "duration_value": "7",
                                        "duration_unit": "MONTHS", "members": "2", "service_fee_percent": "1.42",
                                        "start_date": timezone.localdate().isoformat(), "draw_type": "AUTOMATIC"})
        self.assertEqual(r.status_code, 302)
        from apps.equb.models import EqubGroup

        g = EqubGroup.objects.get(listing=self.listing)
        self.assertEqual(g.service_fee_percent, Decimal("1.42"))

    def test_fee_limits_and_only_staff_may_set_it(self):
        with self.assertRaises(BusinessRuleError):
            product.create_product_equb(self.listing, self.staff, contribution_amount=1000000, duration_value=8,
                                        members=2, start_date=timezone.localdate(), service_fee_percent="25")
        owner = add_staff(self.listing.company, full_name="Abel Owner", phone="0911000777", password="445566",
                          role="OWNER").user
        with self.assertRaises(BusinessRuleError):
            product.create_product_equb(self.listing, owner, contribution_amount=1000000, duration_value=8,
                                        members=2, start_date=timezone.localdate(), service_fee_percent="1")


class CompanyListingPhotosTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.owner = add_staff(self.company, full_name="Abel Owner", phone="0911000888", password="445566",
                               role="OWNER").user
        self.api = client_for(self.owner)
        self.listing = make_listing(company=self.company)

    def test_gallery_sets_photos_in_order_and_cover(self):
        urls = ["https://img.example/a.jpg", "https://img.example/b.jpg", "https://img.example/c.jpg"]
        r = self.api.patch(f"/api/company/listings/{self.listing.pk}/", {"gallery": urls}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([i["url"] for i in r.data["images"]], urls)
        self.assertEqual(r.data["image_url"], urls[0])
        r = self.api.patch(f"/api/company/listings/{self.listing.pk}/", {"gallery": [urls[2], urls[0]]}, format="json")
        self.assertEqual([i["url"] for i in r.data["images"]], [urls[2], urls[0]])
        self.assertEqual(r.data["image_url"], urls[2])

    def test_upload_photo(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buf, "JPEG")
        f = SimpleUploadedFile("p.jpg", buf.getvalue(), content_type="image/jpeg")
        with mock.patch("apps.market.uploads.upload_image", return_value="https://res.cloudinary.com/x/p.jpg"):
            r = self.api.post("/api/company/uploads/photo/", {"file": f}, format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["url"], "https://res.cloudinary.com/x/p.jpg")


class ProductEqubVisibilityTests(TestCase):
    """Product Equbs show in the "Products" package, on the listing (app + console) – also once they've started."""

    def setUp(self):
        self.staff = make_user("Ops", staff=True)
        self.listing = make_listing(price=Decimal("6900000"), units=3, company=make_company())
        self.group, _ = product.create_product_equb(
            self.listing, self.staff, contribution_amount="1005830.91", duration_value=7, duration_unit="MONTHS",
            members=3, start_date=timezone.localdate())

    def test_belongs_to_products_package_listed_in_the_app(self):
        self.assertEqual(self.group.equb_package.slug, "products")
        api = client_for(make_user("Buyer"))
        pkgs = api.get("/api/equb/packages/").data
        self.assertIn("products", [p["slug"] for p in pkgs])
        rows = api.get("/api/equb/groups/?package=products").data
        rows = rows.get("results", rows) if isinstance(rows, dict) else rows
        self.assertEqual(rows[0]["product"]["slug"], self.listing.slug)

    def test_running_group_with_places_still_shows_as_a_way_to_pay(self):
        from apps.equb import services

        services.start_group(self.group)
        self.listing.refresh_from_db()
        self.assertTrue(self.listing.product_equb_open)
        rows = client_for(make_user("Buyer")).get(f"/api/market/listings/{self.listing.slug}/").data["equb_groups"]
        self.assertEqual(rows[0]["slug"], self.group.slug)
        self.assertTrue(rows[0]["running"])
        self.assertEqual((rows[0]["joined"], rows[0]["spots_left"]), (0, 3))

    def test_console_listing_page_shows_the_equb_and_its_numbers(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse("console:listing", args=[self.listing.pk]))
        self.assertContains(r, self.group.name)
        self.assertContains(r, "0 / 3")
        self.assertContains(r, "3 in Equb")
        self.assertContains(r, "taking members")
