"""Chat end to end: app (JSON + multipart), company website, staff console – messages really reach the other side."""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.agencies.services import add_staff
from apps.core.testutils import client_for, company_owner, make_company, make_listing, make_user
from apps.market.models import Inquiry

from .models import Conversation

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


class ChatEndToEndTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.owner = company_owner(self.company)
        self.listing = make_listing(company=self.company, units=2, price=Decimal("100000"))
        self.buyer = make_user("Meron Tadesse")
        self.api = client_for(self.buyer)
        self.staff = make_user("Nebret Ops", staff=True)

    def conv(self):
        return self.api.post("/api/chat/conversations/", {"listing": self.listing.slug}, format="json").data["id"]

    def test_buyer_json_message_reaches_company_app_and_reply_comes_back(self):
        cid = self.conv()
        r = self.api.post(f"/api/chat/conversations/{cid}/messages/", {"text": "Is it still available? 🙂"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        seller = client_for(self.owner)
        rows = seller.get("/api/chat/conversations/").data
        self.assertEqual(rows[0]["id"], cid)
        self.assertEqual(rows[0]["unread"], 1)
        detail = seller.get(f"/api/chat/conversations/{cid}/").data
        self.assertEqual(detail["messages"][-1]["text"], "Is it still available? 🙂")
        reply = seller.post(f"/api/chat/conversations/{cid}/messages/", {"text": "Yes, come on Saturday"}, format="json")
        self.assertEqual(reply.status_code, 201)
        after = r.data["id"]
        new = self.api.get(f"/api/chat/conversations/{cid}/?after={after}").data["messages"]
        self.assertEqual([m["text"] for m in new], ["Yes, come on Saturday"])
        self.assertFalse(new[0]["mine"])

    def test_company_website_messages_page_lists_and_replies(self):
        cid = self.conv()
        self.api.post(f"/api/chat/conversations/{cid}/messages/", {"text": "Hello"}, format="json")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("agency:chats"))
        self.assertContains(page, "Meron Tadesse")
        self.assertContains(page, "Waiting")
        r = self.client.post(reverse("agency:chat", args=[cid]), {"text": "Hi Meron 👋"}, **AJAX)
        self.assertEqual(r.json(), {"ok": True})
        part = self.client.get(reverse("agency:chat", args=[cid]), **AJAX)
        self.assertContains(part, "Hi Meron 👋")
        self.assertContains(part, 'class="ct-copy"')
        msgs = self.api.get(f"/api/chat/conversations/{cid}/").data["messages"]
        self.assertEqual(msgs[-1]["text"], "Hi Meron 👋")

    def test_other_company_cant_open_the_chat(self):
        cid = self.conv()
        other = company_owner(make_company(name="Other Co"))
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("agency:chat", args=[cid])).status_code, 404)

    def test_inquiry_reply_on_company_website_lands_in_the_chat(self):
        r = self.api.post(f"/api/market/listings/{self.listing.slug}/inquire/",
                          {"kind": "QUESTION", "message": "Is parking included?"}, format="json")
        self.assertIn(r.status_code, (200, 201), r.data)
        inq = Inquiry.objects.get(listing=self.listing)
        self.client.force_login(self.owner)
        self.client.post(reverse("agency:inquiries"), {"inquiry": inq.pk, "reply": "Yes, one parking space"})
        conv = Conversation.objects.get(buyer=self.buyer, listing=self.listing)
        msgs = self.api.get(f"/api/chat/conversations/{conv.pk}/").data["messages"]
        self.assertEqual(msgs[-1]["text"], "Yes, one parking space")

    def test_support_message_shows_in_console_needs_reply_and_staff_reply_reaches_buyer(self):
        cid = self.api.post("/api/chat/conversations/", {"support": True}, format="json").data["id"]
        self.api.post(f"/api/chat/conversations/{cid}/messages/", {"text": "I need help"}, format="json")
        self.client.force_login(self.staff)
        inbox = self.client.get(reverse("console:conversations"))
        self.assertContains(inbox, f"#{cid}")
        r = self.client.post(reverse("console:conversation", args=[cid]), {"action": "send", "text": "Hello, how can we help?"}, **AJAX)
        self.assertEqual(r.json(), {"ok": True})
        msgs = self.api.get(f"/api/chat/conversations/{cid}/").data["messages"]
        self.assertEqual(msgs[-1]["text"], "Hello, how can we help?")
        self.assertNotContains(self.client.get(reverse("console:conversations")), f"#{cid}<")

    def test_company_page_in_console_lists_chats_and_quick_reply_works(self):
        cid = self.conv()
        self.api.post(f"/api/chat/conversations/{cid}/messages/", {"text": "Price negotiable?"}, format="json")
        self.client.force_login(self.staff)
        page = self.client.get(reverse("console:agency", args=[self.company.pk]))
        self.assertContains(page, "Chats with buyers")
        self.assertContains(page, "Waiting for reply")
        r = self.client.post(reverse("console:conversation", args=[cid]),
                             {"action": "send", "text": "Nebret here – the seller will answer soon",
                              "next": reverse("console:agency", args=[self.company.pk])})
        self.assertRedirects(r, reverse("console:agency", args=[self.company.pk]), fetch_redirect_response=False)
        texts = [m["text"] for m in self.api.get(f"/api/chat/conversations/{cid}/").data["messages"]]
        self.assertIn("Nebret here – the seller will answer soon", texts)


class SellerChangeTests(TestCase):
    def test_chat_moves_with_the_listing_to_the_new_seller(self):
        old_co, new_co = make_company(name="Old Seller"), make_company(name="Moenco Motors")
        old_owner, new_owner = company_owner(old_co), company_owner(new_co)
        listing = make_listing(company=old_co, units=1)
        buyer = make_user("Meron Tadesse")
        api = client_for(buyer)
        cid = api.post("/api/chat/conversations/", {"listing": listing.slug}, format="json").data["id"]
        api.post(f"/api/chat/conversations/{cid}/messages/", {"text": "Hello"}, format="json")
        with self.captureOnCommitCallbacks(execute=True):
            listing.company = new_co
            listing.save()
        conv = Conversation.objects.get(pk=cid)
        self.assertEqual(conv.agency_id, new_co.pk)
        self.assertNotEqual(client_for(old_owner).get(f"/api/chat/conversations/{cid}/").status_code, 200)
        seller_view = client_for(new_owner).get(f"/api/chat/conversations/{cid}/").data
        self.assertEqual(seller_view["messages"][-1]["card"]["type"], "SELLER_CHANGED")
        self.assertEqual(api.get("/api/chat/conversations/").data[0]["title"], "Moenco Motors")
