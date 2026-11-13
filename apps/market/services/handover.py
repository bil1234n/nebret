"""
Signed handover pass.

When an order is paid in full the customer gets a pass (shown as a QR code in the app). The seller scans or types it at
handover; the signature proves the pass was issued by Nebret Equb for that exact order, so it can't be forged.
"""
from __future__ import annotations

import hashlib
import hmac

from django.conf import settings

from ..models import Order


def _key() -> bytes:
    return str(settings.NEBRET["TICKET_SIGNING_KEY"]).encode()


def signature(order: Order) -> str:
    msg = f"{order.reference}:{order.user_id}:{order.unit_id or 0}:{order.total_amount}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()[:16].upper()


def pass_payload(order: Order) -> str:
    return f"NEBRET:{order.reference}:{signature(order)}"


def verify_pass(payload: str) -> Order | None:
    """``NEBRET:<reference>:<signature>`` (or just the reference, typed by staff) → the order, if genuine."""
    text = (payload or "").strip().upper()
    if text.startswith("NEBRET:"):
        parts = text.split(":")
        if len(parts) != 3:
            return None
        order = Order.objects.filter(reference=parts[1]).select_related("listing", "unit", "user").first()
        if order and hmac.compare_digest(signature(order), parts[2]):
            return order
        return None
    return Order.objects.filter(reference=text).select_related("listing", "unit", "user").first()
