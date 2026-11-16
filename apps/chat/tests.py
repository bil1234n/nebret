"""In-app chat: moderation, who can read, contact details never exposed, inquiries and orders in chat."""
import json
from decimal import Decimal

from django.test import TestCase

from apps.agencies.services import add_staff
from apps.core.testutils import client_for, company_owner, make_company, make_listing, make_user
from apps.market.services import orders
from apps.payments.models import FraudAlert

from .models import Conversation, Message, Restriction
from .moderation import MASK, screen


class ModerationTests(TestCase):
    def test_contact_details_are_masked(self):
        cases = ["call 0911234567", "+251 91 123 4567", "0 9 1 1 2 3 4 5 6 7", "09-11-23-45-67",
                 "zero nine one one two three four five six seven", "ዜሮ ዘጠኝ አንድ አንድ ሁለት ሶስት አራት አምስት ስድስት ሰባት",
                 "፱፩፩፪፫፬፭፮፯", "abebe@gmail.com", "abebe at gmail dot com", "telegram @abebe_sells", "t.me/abebe",
                 "wa.me/251911234567", "viber abebe123", "pay into my account 1000123456789", "+44 20 7946 0958"]
        for text in cases:
            r = screen(text)
            self.assertTrue(r.masked, text)
            self.assertIn(MASK, r.text, text)
            self.assertFalse(any(ch.isdigit() for ch in r.text.replace("251", "")) and "@" in r.text, text)

    def test_normal_messages_and_prices_are_left_alone(self):
        for text in ["The price is 3,000,000 ETB", "Can I visit on Saturday at 10:30?", "Unit 3B, 4th floor",
                     "Down payment 900,000.00 birr", "I paid with payment code PK7Q9M2"]:
            self.assertFalse(screen(text).masked, text)

    def test_official_order_account_is_allowed(self):
        self.assertFalse(screen("Paid to 1000555000111", allowed_numbers=["1000555000111"]).masked)
        self.assertTrue(screen("Pay to 1000555000999 instead", allowed_numbers=["1000555000111"]).masked)


class ChatFlowTests(TestCase):
    def setUp(self):
        self.company = make_company()
        self.owner = company_owner(self.company)
        self.agent = add_staff(self.company, full_name="Abebe Kebede", phone="0911888001", role="AGENT").user
        self.listing = make_listing(company=self.company, units=2, price=Decimal("100000"))
        self.buyer = make_user("Meron Tadesse")
        self.api = client_for(self.buyer)

    def open(self):
        r = self.api.post("/api/chat/conversations/", {"listing": self.listing.slug}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return r.data["id"]

    def test_conversation_messages_labels_masking_and_unread(self):
        cid = self.open()
        self.assertEqual(self.open(), cid)  # one conversation per buyer + listing
        with self.captureOnCommitCallbacks(execute=True):
            r = self.api.post(f"/api/chat/conversations/{cid}/messages/", {"text": "Call me on 0911 22 33 44"},
                              format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["masked"])
        self.assertNotIn("0911", r.data["text"])
        self.assertIn("contact details are hidden", r.data["notice"])
        self.assertEqual(Message.objects.get(pk=r.data["id"]).original_text, "Call me on 0911 22 33 44")
        self.assertTrue(self.agent.notifications.filter(kind="CHAT").exists())  # push to company staff
        agent_api = client_for(self.agent)
        self.assertEqual(agent_api.get("/api/chat/unread/").data["total"], 1)
        r = agent_api.post(f"/api/chat/conversations/{cid}/messages/", {"text": "Yes, it's available."},
                           format="multipart")
        self.assertEqual(r.data["sender"], "Sunshine Real Estate – Abebe (Sales)")
        d = self.api.get(f"/api/chat/conversations/{cid}/").data
        self.assertEqual(len(d["messages"]), 2)
        self.assertNotIn("original_text", d["messages"][0])  # the original is for Nebret only
        self.assertEqual(agent_api.get("/api/chat/unread/").data["total"], 0)
        # polling: only newer messages
        last = d["messages"][-1]["id"]
        self.assertEqual(self.api.get(f"/api/chat/conversations/{cid}/?after={last}").data["messages"], [])

    def test_non_participants_cant_read(self):
        cid = self.open()
        self.api.post(f"/api/chat/conversations/{cid}/messages/", {"text": "Hello"}, format="multipart")
        stranger = client_for(make_user("Stranger"))
        self.assertEqual(stranger.get(f"/api/chat/conversations/{cid}/").status_code, 409)
        self.assertEqual(stranger.post(f"/api/chat/conversations/{cid}/messages/", {"text": "hi"}).status_code, 409)
        other = make_company(name="Other Motors")
        other_staff = company_owner(other)
        self.assertEqual(client_for(other_staff).get(f"/api/chat/conversations/{cid}/").status_code, 409)
        # Nebret staff only after joining (both sides see it) or when it is reported
        staff = client_for(make_user("Nebret Ops", staff=True))
        self.assertEqual(staff.get(f"/api/chat/conversations/{cid}/").status_code, 409)
        self.api.post(f"/api/chat/conversations/{cid}/report/", {"reason": "Asked me to pay outside"}, format="json")
        self.assertEqual(staff.get(f"/api/chat/conversations/{cid}/").status_code, 200)

    def test_violations_alert_staff_and_mute_blocks_sending(self):
        cid = self.open()
        staff_user = make_user("Nebret Ops", staff=True)
        for i in range(3):
            self.api.post(f"/api/chat/conversations/{cid}/messages/", {"text": f"my number 09112233{i}4"},
                          format="multipart")
        self.assertTrue(FraudAlert.objects.filter(kind="CHAT_VIOLATIONS", user=self.buyer).exists())
        Restriction.objects.create(user=self.buyer, level="MUTED", by=staff_user)
        r = self.api.post(f"/api/chat/conversations/{cid}/messages/", {"text": "hello"}, format="multipart")
        self.assertEqual(r.status_code, 409)

    def test_inquiry_becomes_conversation_with_card_and_order_links_it(self):
        r = self.api.post(f"/api/market/listings/{self.listing.slug}/inquire/",
                          {"kind": "VIEWING", "message": "Can I see it Saturday?", "preferred_date": "2026-12-05"},
                          format="json")
        self.assertEqual(r.status_code, 201, r.data)
        conv = Conversation.objects.get(buyer=self.buyer, listing=self.listing)
        card = conv.messages.get().card
        self.assertEqual((card["type"], card["preferred_date"]), ("VIEWING_REQUEST", "2026-12-05"))
        with self.captureOnCommitCallbacks(execute=True):
            order = orders.create_order(user=self.buyer, listing=self.listing, plan="FULL")
        conv.refresh_from_db()
        self.assertEqual(conv.order, order)
        self.assertTrue(conv.messages.filter(card__type="ORDER_PLACED").exists())

    def test_support_conversation(self):
        r = self.api.post("/api/chat/conversations/", {"support": True}, format="json")
        self.assertEqual(r.data["kind"], "SUPPORT")
        staff = client_for(make_user("Nebret Ops", staff=True))
        self.assertEqual(staff.get(f"/api/chat/conversations/{r.data['id']}/").status_code, 200)


class NoContactDetailsTests(TestCase):
    """The company's phone / email / website never reach a buyer; only Nebret's support phone does."""

    def test_company_contact_absent_from_buyer_responses(self):
        company = make_company(email="sales@sunshine.et", website="https://sunshine.et")
        listing = make_listing(company=company, units=2)
        buyer = make_user("Meron Tadesse")
        api = client_for(buyer)
        order = orders.create_order(user=buyer, listing=listing, plan="FULL")
        cid = api.post("/api/chat/conversations/", {"listing": listing.slug}, format="json").data["id"]
        responses = [
            api.get("/api/market/listings/"), api.get(f"/api/market/listings/{listing.slug}/"),
            api.get("/api/market/companies/"), api.get(f"/api/market/companies/{company.slug}/"),
            api.get("/api/market/orders/"), api.get(f"/api/market/orders/{order.reference}/"),
            api.get(f"/api/market/orders/{order.reference}/bank-instructions/"),
            api.get("/api/chat/conversations/"), api.get(f"/api/chat/conversations/{cid}/"),
            api.get("/api/home/dashboard/"),
        ]
        for r in responses:
            self.assertEqual(r.status_code, 200, r.wsgi_request.path)
            body = json.dumps(r.data, default=str)
            for secret in (company.phone, "911000000", "sales@sunshine.et", "sunshine.et"):
                self.assertNotIn(secret, body, r.wsgi_request.path)
        detail = api.get(f"/api/market/listings/{listing.slug}/").data
        self.assertTrue(detail["support_phone"])
        self.assertNotIn("phone", detail["company"])
