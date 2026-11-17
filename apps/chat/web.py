"""
Chat in the web pages (staff console and company portal): one shared thread – bubbles, copy, emoji, attachments,
Enter to send, refreshed every few seconds without reloading the page.
"""
from __future__ import annotations

from django.contrib import messages as flash
from django.http import JsonResponse
from django.shortcuts import redirect, render

from apps.core.exceptions import BusinessRuleError

from . import services
from .models import Conversation, Message


def thread_rows(conv: Conversation, user, request, *, staff_view: bool = False) -> list[dict]:
    rows = [services.message_row(m, user, request=request, staff_view=staff_view)
            for m in conv.messages.select_related("sender").order_by("created_at", "id")]
    prev_day = None
    for r in rows:
        day = r["created_at"].date()
        r["new_day"] = day != prev_day
        prev_day = day
    return rows


def is_ajax(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def send_from_web(request, conv: Conversation, redirect_to: str):
    """POST ``text`` / ``files`` from a web page; JSON for the page's script, a redirect otherwise."""
    try:
        services.post_message(conv, request.user, request.POST.get("text", ""), request.FILES.getlist("files"))
    except BusinessRuleError as exc:
        if is_ajax(request):
            return JsonResponse({"ok": False, "error": str(exc.detail)}, status=400)
        flash.error(request, str(exc.detail))
        return redirect(redirect_to)
    if is_ajax(request):
        return JsonResponse({"ok": True})
    return redirect(redirect_to)


def render_messages(request, conv: Conversation, *, staff_view: bool = False):
    """Only the message list – the page polls this every few seconds."""
    services.mark_read(conv, request.user)
    return render(request, "chat/_messages.html", {"msgs": thread_rows(conv, request.user, request, staff_view=staff_view),
                                                   "staff_view": staff_view})


def unanswered(conv: Conversation) -> bool:
    last = conv.messages.filter(deleted_at__isnull=True).order_by("-created_at", "-id").first()
    return bool(last and last.sender_role == Message.Role.BUYER)
