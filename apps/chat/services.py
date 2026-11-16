"""
Chat rules: who is in a conversation, posting (with moderation), read receipts, system cards, reports and moderation
actions. Delivery is polling (the app refreshes every few seconds while a chat is open) plus an Expo push per message –
Vercel has no WebSockets.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify

from . import moderation
from .models import Conversation, Message, ReadState, Report, Restriction, Violation

log = logging.getLogger(__name__)
C = Conversation


# =========================================================================== who
def agency_member(user, agency_id):
    if not agency_id or not getattr(user, "is_authenticated", False):
        return None
    from apps.agencies.models import AgencyStaff

    return AgencyStaff.objects.select_related("agency").filter(user=user, agency_id=agency_id, is_active=True).first()


def role_in(conv: Conversation, user) -> str | None:
    if not getattr(user, "is_authenticated", False):
        return None
    if conv.buyer_id == user.id:
        return Message.Role.BUYER
    if conv.agency_id and agency_member(user, conv.agency_id):
        return Message.Role.AGENCY
    if user.is_staff:
        return Message.Role.NEBRET
    return None


def staff_may_read(conv: Conversation) -> bool:
    """Nebret staff read a conversation when it is a support chat, they joined it, it was reported, it has flagged
    messages or its order has a disputed payment (stated in the privacy policy)."""
    if conv.kind == C.Kind.SUPPORT or conv.support_joined or not conv.agency_id:
        return True
    if conv.reports.exists() or conv.messages.filter(was_masked=True).exists():
        return True
    return bool(conv.order_id and conv.order.proofs.filter(status="DISPUTED").exists())


def can_read(conv: Conversation, user) -> bool:
    role = role_in(conv, user)
    if role == Message.Role.NEBRET:
        return staff_may_read(conv)
    if role is None:
        return False
    return active_restriction(user, Restriction.Level.SUSPENDED) is None


def visible_to(user):
    if user.is_staff:
        return Conversation.objects.all()
    from apps.agencies.models import AgencyStaff

    agency_ids = list(AgencyStaff.objects.filter(user=user, is_active=True).values_list("agency_id", flat=True))
    return Conversation.objects.filter(Q(buyer=user) | Q(agency_id__in=agency_ids))


def active_restriction(user, *levels):
    now = timezone.now()
    qs = Restriction.objects.filter(user=user, lifted_at__isnull=True).filter(Q(until__isnull=True) | Q(until__gt=now))
    if levels:
        qs = qs.filter(level__in=levels)
    return qs.first()


def sender_label(conv: Conversation, user, role: str) -> str:
    if role == Message.Role.AGENCY:
        member = agency_member(user, conv.agency_id)
        short = {"OWNER": "Owner", "MANAGER": "Manager", "AGENT": "Sales"}.get(member.role, "") if member else ""
        first = (user.full_name or "").split(" ")[0]
        return f"{conv.agency.name} – {first}" + (f" ({short})" if short else "")
    if role == Message.Role.NEBRET:
        return f"Nebret support – {(user.full_name or '').split(' ')[0]}"
    if role == Message.Role.SYSTEM:
        return "Nebret"
    return user.full_name if user else "Buyer"


# =========================================================================== conversations
def for_listing(buyer, listing) -> Conversation:
    if listing.company_id and agency_member(buyer, listing.company_id):
        raise BusinessRuleError("You can't open a chat with your own company.")
    conv = Conversation.objects.filter(kind=C.Kind.SELLER, buyer=buyer, listing=listing).first()
    if conv:
        return conv
    try:
        with transaction.atomic():
            return Conversation.objects.create(kind=C.Kind.SELLER, buyer=buyer, listing=listing,
                                               agency_id=listing.company_id)
    except IntegrityError:
        return Conversation.objects.get(kind=C.Kind.SELLER, buyer=buyer, listing=listing)


def for_order(order) -> Conversation:
    conv = for_listing(order.user, order.listing)
    if conv.order_id != order.pk:
        conv.order = order
        conv.save(update_fields=["order", "updated_at"])
    return conv


def support_for(user, order=None) -> Conversation:
    conv = Conversation.objects.filter(kind=C.Kind.SUPPORT, buyer=user, status=C.Status.OPEN).order_by("-id").first()
    if not conv:
        conv = Conversation.objects.create(kind=C.Kind.SUPPORT, buyer=user, order=order, support_joined=True)
    return conv


def link_order(order_id: int):
    """A new order links (or opens) the buyer↔seller conversation and posts an order card."""
    from apps.market.models import Order

    try:
        order = Order.objects.select_related("listing", "user").get(pk=order_id)
        conv = for_order(order)
        post_card(conv, "ORDER_PLACED", {"order": order.reference, "plan": order.get_plan_display(),
                                         "total": str(order.total_amount), "status": order.status},
                  f"Order {order.reference} placed – {order.get_plan_display()}.")
    except Exception:  # noqa: BLE001 – chat must never break checkout
        log.exception("Could not link order %s to a conversation", order_id)


def conversation_from_inquiry(inquiry) -> Conversation:
    """Inquiries (questions, viewing / test-drive requests) now start a conversation with a structured card."""
    conv = for_listing(inquiry.user, inquiry.listing)
    screened = moderation.screen(inquiry.message)
    msg = Message.objects.create(
        conversation=conv, sender=inquiry.user, sender_role=Message.Role.BUYER,
        sender_label=inquiry.user.full_name, kind=Message.Kind.CARD if inquiry.kind != "QUESTION" else Message.Kind.TEXT,
        text=screened.text, original_text=inquiry.message if screened.masked else "", was_masked=screened.masked,
        card=({"type": f"{inquiry.kind}_REQUEST", "kind_label": inquiry.get_kind_display(),
               "preferred_date": inquiry.preferred_date.isoformat() if inquiry.preferred_date else None,
               "inquiry": inquiry.pk} if inquiry.kind != "QUESTION" else {}))
    _after_post(conv, msg, inquiry.user, Message.Role.BUYER, screened)
    return conv


# =========================================================================== messages
def post_message(conv: Conversation, user, text: str = "", files=None, *, card: dict | None = None) -> Message:
    role = role_in(conv, user)
    if role is None:
        raise BusinessRuleError("You are not part of this conversation.")
    if role == Message.Role.NEBRET and not conv.support_joined and conv.agency_id:
        join(conv, user)
    if conv.status != C.Status.OPEN:
        raise BusinessRuleError("This conversation is closed.")
    if role != Message.Role.NEBRET:
        r = active_restriction(user, Restriction.Level.MUTED, Restriction.Level.SUSPENDED)
        if r:
            raise BusinessRuleError("You can't send messages right now"
                                    + (f" (until {timezone.localtime(r.until):%d %b %H:%M})" if r.until else "")
                                    + ". Contact Nebret support.")
    text = (text or "").strip()[:4000]
    files = [f for f in (files or []) if f]
    if not text and not files and not card:
        raise BusinessRuleError("Write a message first.")
    if len(files) > 5:
        raise BusinessRuleError("Attach at most 5 files.")
    attachments = []
    if files:
        from apps.core import private_files

        for f in files:
            data, ctype = private_files.validate(f)
            attachments.append(private_files.store(data, ctype, folder=f"chat/{conv.pk}", name=getattr(f, "name", "")))
    allowed = []
    if conv.order_id and conv.order.pay_to_account:
        allowed.append(conv.order.pay_to_account.get("account_number", ""))
    screened = moderation.screen(text, allowed_numbers=allowed)
    msg = Message.objects.create(
        conversation=conv, sender=user, sender_role=role, sender_label=sender_label(conv, user, role),
        kind=Message.Kind.CARD if card else Message.Kind.TEXT, text=screened.text,
        original_text=text if screened.masked else "", card=card or {}, attachments=attachments,
        was_masked=screened.masked)
    _after_post(conv, msg, user, role, screened)
    return msg


def _after_post(conv: Conversation, msg: Message, user, role: str, screened):
    now = msg.created_at
    updates = {"last_message_at": now}
    if role == Message.Role.BUYER and not conv.first_buyer_message_at:
        updates["first_buyer_message_at"] = now
    if role == Message.Role.AGENCY and conv.first_buyer_message_at and not conv.first_agency_reply_at:
        updates["first_agency_reply_at"] = now
    Conversation.objects.filter(pk=conv.pk).update(**updates)
    for k, v in updates.items():
        setattr(conv, k, v)
    if user is not None:
        mark_read(conv, user, at=now)
    if screened is not None and screened.masked:
        _record_violation(conv, msg, user, screened.kinds)
    _push(conv, msg, user)


def _record_violation(conv, msg, user, kinds):
    agency_id = conv.agency_id if msg.sender_role == Message.Role.AGENCY else None
    Violation.objects.create(user=user, agency_id=agency_id, message=msg, kinds=sorted(set(kinds)))
    n = Violation.objects.filter(user=user).count()
    limit = settings.NEBRET["CHAT_VIOLATIONS_ALERT"]
    if n and n % limit == 0:
        from apps.accounts.models import User
        from apps.payments.models import FraudAlert

        alert = FraudAlert.objects.create(
            kind=FraudAlert.Kind.CHAT_VIOLATIONS, user=user, agency_id=agency_id,
            message=f"{user.full_name} has tried to share contact details {n} times in chat (latest in "
                    f"conversation #{conv.pk}).", data={"conversation": conv.pk, "count": n})
        for staff in User.objects.filter(is_staff=True, is_active=True):
            notify(staff, Notification.Kind.PAYMENT, "Nebret alert: contact details in chat", alert.message,
                   {"fraud_alert": alert.pk, "conversation": conv.pk})


def recipients(conv: Conversation, exclude=None) -> list:
    from apps.accounts.models import User

    users = [conv.buyer]
    if conv.agency_id:
        from apps.agencies.models import AgencyStaff

        users += [s.user for s in AgencyStaff.objects.filter(agency_id=conv.agency_id, is_active=True)
                  .select_related("user")]
    if conv.kind == C.Kind.SUPPORT or conv.support_joined or not conv.agency_id:
        users += list(User.objects.filter(is_staff=True, is_active=True))
    seen, out = set(), []
    for u in users:
        if u and u.pk not in seen and (exclude is None or u.pk != exclude.pk):
            seen.add(u.pk)
            out.append(u)
    return out


def _push(conv: Conversation, msg: Message, sender):
    title = msg.sender_label or "New message"
    body = msg.text[:180] or ("📎 Attachment" if msg.attachments else msg.card.get("title", "Update"))
    data = {"conversation": conv.pk, **({"order": conv.order.reference} if conv.order_id else {}),
            **({"listing": conv.listing.slug} if conv.listing_id else {})}
    users = recipients(conv, exclude=sender)
    transaction.on_commit(lambda: [notify(u, Notification.Kind.CHAT, title[:120], body[:500], data) for u in users])


def post_card(conv: Conversation, card_type: str, data: dict, text: str) -> Message:
    msg = Message.objects.create(conversation=conv, sender=None, sender_role=Message.Role.SYSTEM, sender_label="Nebret",
                                 kind=Message.Kind.CARD, text=text[:4000], card={"type": card_type, **data})
    _after_post(conv, msg, None, Message.Role.SYSTEM, None)
    return msg


def mark_read(conv: Conversation, user, at=None):
    at = at or timezone.now()
    ReadState.objects.update_or_create(conversation=conv, user=user, defaults={"last_read_at": at})


def unread_for(conv: Conversation, user) -> int:
    state = ReadState.objects.filter(conversation=conv, user=user).first()
    qs = conv.messages.exclude(sender=user).filter(deleted_at__isnull=True)
    if state:
        qs = qs.filter(created_at__gt=state.last_read_at)
    return qs.count()


def join(conv: Conversation, staff):
    if not staff.is_staff:
        raise BusinessRuleError("Only Nebret staff can join conversations.")
    if not conv.support_joined:
        Conversation.objects.filter(pk=conv.pk).update(support_joined=True)
        conv.support_joined = True
        post_card(conv, "SUPPORT_JOINED", {"by": (staff.full_name or "").split(" ")[0]},
                  f"Nebret support ({(staff.full_name or '').split(' ')[0]}) joined the conversation.")
    return conv


def edit_message(msg: Message, user, text: str) -> Message:
    if msg.sender_id != user.id or msg.deleted_at:
        raise BusinessRuleError("You can only edit your own messages.")
    if (timezone.now() - msg.created_at).total_seconds() > 15 * 60:
        raise BusinessRuleError("Messages can be edited for 15 minutes.")
    allowed = []
    conv = msg.conversation
    if conv.order_id and conv.order.pay_to_account:
        allowed.append(conv.order.pay_to_account.get("account_number", ""))
    screened = moderation.screen(text.strip()[:4000], allowed_numbers=allowed)
    msg.text, msg.edited_at = screened.text, timezone.now()
    if screened.masked:
        msg.original_text, msg.was_masked = text, True
        _record_violation(conv, msg, user, screened.kinds)
    msg.save(update_fields=["text", "edited_at", "original_text", "was_masked"])
    return msg


def delete_message(msg: Message, user) -> Message:
    if msg.sender_id != user.id:
        raise BusinessRuleError("You can only delete your own messages.")
    msg.deleted_at = timezone.now()
    msg.save(update_fields=["deleted_at"])
    return msg


def report(conv: Conversation, user, reason: str) -> Report:
    if role_in(conv, user) in (None, Message.Role.NEBRET):
        raise BusinessRuleError("You are not part of this conversation.")
    if not (reason or "").strip():
        raise BusinessRuleError("Tell us what happened.")
    r = Report.objects.create(conversation=conv, reporter=user, reason=reason.strip()[:500])
    from apps.accounts.models import User
    from apps.payments.models import FraudAlert

    alert = FraudAlert.objects.create(kind=FraudAlert.Kind.CHAT_VIOLATIONS, user=user, agency_id=conv.agency_id,
                                      message=f"Conversation #{conv.pk} reported: {reason.strip()}"[:255],
                                      data={"conversation": conv.pk, "report": r.pk})
    for staff in User.objects.filter(is_staff=True, is_active=True):
        notify(staff, Notification.Kind.PAYMENT, "Chat reported", alert.message, {"conversation": conv.pk})
    return r


def restrict(user, staff, level: str, *, days: int | None = None, reason: str = "") -> Restriction:
    if not staff.is_staff:
        raise BusinessRuleError("Only Nebret staff can do this.")
    if level not in Restriction.Level.values:
        raise BusinessRuleError("Choose warn, mute or suspend.")
    until = timezone.now() + timedelta(days=days) if days else None
    r = Restriction.objects.create(user=user, level=level, until=until, reason=reason[:255], by=staff)
    text = {"WARNED": "Nebret warning: please keep all contact and payments inside the app.",
            "MUTED": "You can't send chat messages for now because contact details were shared.",
            "SUSPENDED": "Your chat access is suspended. Contact Nebret support."}[level]
    notify(user, Notification.Kind.CHAT, "Message from Nebret", text + (f" {reason}" if reason else ""), {})
    return r


def lift(restriction: Restriction, staff):
    restriction.lifted_at = timezone.now()
    restriction.save(update_fields=["lifted_at", "updated_at"])
    return restriction


# =========================================================================== order → chat cards
CARD_EVENTS = {"CONFIRMED", "PAID_IN_FULL", "PARTNER_CONFIRMED", "PROOF_REJECTED", "HANDED_OVER", "CANCELLED",
               "AGREEMENT_ISSUED", "INSTALLMENT"}


def on_order_event(sender, instance, created, **kwargs):
    if not created or instance.kind not in CARD_EVENTS:
        return
    order_id, kind, message = instance.order_id, instance.kind, instance.message
    transaction.on_commit(lambda: _order_card(order_id, kind, message))


def _order_card(order_id, kind, message):
    from apps.market.models import Order

    try:
        order = Order.objects.select_related("listing", "user").get(pk=order_id)
        conv = order.conversations.order_by("-id").first() or for_order(order)
        card_type = {"PARTNER_CONFIRMED": "PAYMENT_CONFIRMED"}.get(kind, f"ORDER_{kind}")
        post_card(conv, card_type, {"order": order.reference, "status": order.status,
                                    "status_label": order.get_status_display(), "paid": str(order.amount_paid),
                                    "balance": str(order.balance_due)}, message)
    except Exception:  # noqa: BLE001
        log.exception("Could not post order card for %s", order_id)


# =========================================================================== rows
def conversation_row(conv: Conversation, user, *, request=None) -> dict:
    role = role_in(conv, user)
    last = conv.messages.filter(deleted_at__isnull=True).order_by("-created_at", "-id").first()
    listing = conv.listing
    image = ""
    if listing:
        image = listing.image_url or (listing.images.first().image_url if listing.images.exists() else "")
    if role == Message.Role.BUYER:
        title = "Nebret support" if conv.kind == C.Kind.SUPPORT else (conv.agency.name if conv.agency_id else "Nebret")
    else:
        title = conv.buyer.full_name
    return {"id": conv.pk, "kind": conv.kind, "status": conv.status, "title": title,
            "listing": {"slug": listing.slug, "title": listing.title, "image_url": image} if listing else None,
            "order": conv.order.reference if conv.order_id else None,
            "last_message": ({"text": last.text[:140] or ("📎" if last.attachments else ""), "at": last.created_at,
                              "from_me": last.sender_id == user.id, "role": last.sender_role} if last else None),
            "last_message_at": conv.last_message_at, "unread": unread_for(conv, user) if role else 0,
            "my_role": role, "support_joined": conv.support_joined}


def message_row(msg: Message, user, *, request=None, staff_view: bool = False) -> dict:
    from apps.core import private_files

    deleted = msg.deleted_at is not None
    row = {"id": msg.pk, "role": msg.sender_role, "sender": msg.sender_label, "mine": msg.sender_id == user.id,
           "kind": msg.kind, "text": "" if deleted else msg.text, "card": {} if deleted else msg.card,
           "attachments": [] if deleted else [{"name": a.get("name", ""), "content_type": a.get("content_type", ""),
                                               "url": private_files.signed_url(a, request)} for a in msg.attachments],
           "masked": msg.was_masked, "notice": moderation.NOTICE if msg.was_masked else "",
           "created_at": msg.created_at, "edited": msg.edited_at is not None, "deleted": deleted}
    if staff_view and msg.was_masked:
        row["original_text"] = msg.original_text
    return row


def read_receipts(conv: Conversation, user) -> dict:
    """When the other side last read the conversation (for the ✓✓ ticks)."""
    others = ReadState.objects.filter(conversation=conv).exclude(user=user)
    return {"others_last_read_at": max((r.last_read_at for r in others), default=None)}
