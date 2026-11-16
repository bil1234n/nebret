"""
Chat API (``/api/chat/``). Polling: the app calls ``GET conversations/<id>/?after=<last message id>`` every few seconds
while a chat is open and refreshes the list on focus; a push notification is sent for every new message.
"""
from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.core.exceptions import BusinessRuleError

from . import services
from .models import Conversation, Message


def _conv(request, pk) -> Conversation:
    conv = get_object_or_404(Conversation.objects.select_related("agency", "listing", "order", "buyer"), pk=pk)
    if services.role_in(conv, request.user) is None:
        raise BusinessRuleError("You are not part of this conversation.")
    return conv


@api_view(["GET", "POST"])
@permission_classes([permissions.IsAuthenticated])
def conversations(request):
    """GET – my conversations (``?kind=SUPPORT|SELLER``, ``?q=``). POST – open one: ``{listing}``, ``{order}`` or
    ``{support: true}``."""
    if request.method == "POST":
        d = request.data
        from apps.market.models import Listing, Order

        if d.get("order"):
            order = get_object_or_404(Order, reference=str(d["order"]).upper())
            if order.user_id != request.user.id and not services.agency_member(request.user, order.company_id):
                raise BusinessRuleError("That is not your order.")
            conv = services.for_order(order)
        elif d.get("listing"):
            key = str(d["listing"])
            listing = get_object_or_404(Listing, **({"pk": int(key)} if key.isdigit() else {"slug": key}))
            conv = services.for_listing(request.user, listing)
        elif d.get("support"):
            order = None
            if d.get("support_order"):
                order = Order.objects.filter(reference=str(d["support_order"]).upper(), user=request.user).first()
            conv = services.support_for(request.user, order)
        else:
            raise BusinessRuleError("Say which listing or order the conversation is about.")
        return Response(services.conversation_row(conv, request.user, request=request), status=status.HTTP_201_CREATED)
    qs = services.visible_to(request.user).select_related("agency", "listing", "order", "buyer")
    if request.query_params.get("kind"):
        qs = qs.filter(kind=request.query_params["kind"].upper())
    if request.user.is_staff and not request.query_params.get("all"):
        qs = qs.filter(buyer=request.user) | qs.filter(kind=Conversation.Kind.SUPPORT)
    rows = [services.conversation_row(c, request.user, request=request)
            for c in qs.order_by("-last_message_at", "-id")[:200]]
    return Response(rows)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def conversation(request, pk: int):
    conv = _conv(request, pk)
    if not services.can_read(conv, request.user):
        raise BusinessRuleError("Nebret staff can read this conversation only after joining it or when it is "
                                "reported or disputed.")
    qs = conv.messages.select_related("sender")
    after = request.query_params.get("after")
    if after and str(after).isdigit():
        qs = qs.filter(pk__gt=int(after))
    else:
        qs = qs.order_by("-created_at", "-id")[:100][::-1] if qs.count() > 100 else qs
    staff_view = request.user.is_staff
    msgs = [services.message_row(m, request.user, request=request, staff_view=staff_view) for m in qs]
    if msgs:
        services.mark_read(conv, request.user)
    return Response({**services.conversation_row(conv, request.user, request=request), "messages": msgs,
                     **services.read_receipts(conv, request.user), "poll_seconds": settings.NEBRET["CHAT_POLL_SECONDS"],
                     "support_phone": settings.NEBRET["SUPPORT_PHONE"]})


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def messages(request, pk: int):
    """``text`` and/or ``files`` (JPG/PNG/PDF, up to 5)."""
    conv = _conv(request, pk)
    msg = services.post_message(conv, request.user, request.data.get("text", ""), request.FILES.getlist("files"))
    return Response(services.message_row(msg, request.user, request=request), status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([permissions.IsAuthenticated])
def message(request, pk: int, message_id: int):
    conv = _conv(request, pk)
    msg = get_object_or_404(Message, pk=message_id, conversation=conv)
    if request.method == "DELETE":
        msg = services.delete_message(msg, request.user)
    else:
        msg = services.edit_message(msg, request.user, str(request.data.get("text") or ""))
    return Response(services.message_row(msg, request.user, request=request))


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def read(request, pk: int):
    conv = _conv(request, pk)
    services.mark_read(conv, request.user)
    return Response({"ok": True})


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def report(request, pk: int):
    conv = _conv(request, pk)
    services.report(conv, request.user, str(request.data.get("reason") or ""))
    return Response({"ok": True, "message": "Thank you. Nebret support will look at this conversation."},
                    status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([permissions.IsAuthenticated])
def join(request, pk: int):
    conv = get_object_or_404(Conversation, pk=pk)
    services.join(conv, request.user)
    return Response(services.conversation_row(conv, request.user, request=request))


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def unread(request):
    """Badges: total unread, and per conversation / order / listing."""
    qs = services.visible_to(request.user).select_related("order", "listing")
    if request.user.is_staff:
        qs = qs.filter(buyer=request.user) | qs.filter(kind=Conversation.Kind.SUPPORT)
    per, by_order, by_listing, total = {}, {}, {}, 0
    for c in qs:
        n = services.unread_for(c, request.user)
        if not n:
            continue
        per[c.pk] = n
        total += n
        if c.order_id:
            by_order[c.order.reference] = by_order.get(c.order.reference, 0) + n
        if c.listing_id:
            by_listing[c.listing.slug] = by_listing.get(c.listing.slug, 0) + n
    return Response({"total": total, "conversations": per, "orders": by_order, "listings": by_listing})


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def support(request):
    """The only phone number the app shows."""
    return Response({"phone": settings.NEBRET["SUPPORT_PHONE"], "email": settings.NEBRET["SUPPORT_EMAIL"]})
