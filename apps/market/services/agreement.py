"""
Sales & Installment Pre-Agreement (PDF, reportlab).

Issued when the order's first payment stage is confirmed and re-issued as a new version after every later confirmed
payment (old versions are kept and marked superseded). Each PDF carries a QR code with an HMAC signature (same pattern
as the handover pass) that opens a public page showing only: valid / invalid, order reference, company, issue date.

The legal clauses below are placeholders until Nebret's lawyer supplies the final text ([DECIDE] in the task).
"""
from __future__ import annotations

import hashlib
import hmac
import io
import logging

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import BankTransferProof, Order, OrderAgreement, OrderEvent
from .orders import fmt, log_event

log = logging.getLogger(__name__)

LEGAL_CLAUSES = [
    "This pre-agreement records the sale of the item described above between the seller and the buyer, the agreed "
    "price and payment plan, and every payment the seller has confirmed receiving into its own bank account.",
    "All payments are made by bank transfer directly into the seller's account shown in the Nebret app. Nebret Equb "
    "does not hold the buyer's money; it verifies each transfer with the bank and records the seller's confirmation.",
    "Ownership passes to the buyer only at handover after full payment. For immovable property (land, houses, "
    "apartments) the final sale contract must be authenticated at the Documents Authentication and Registration "
    "Service (DARS) or a notary, and registered as required by Ethiopian law. This pre-agreement is not a title deed.",
    "If the order is cancelled, the seller refunds the buyer from its own account under the seller's terms; Nebret "
    "tracks the refund.",
    "[Final legal wording to be supplied by Nebret's legal counsel – placeholder text.]",
]


def _key() -> bytes:
    return str(settings.NEBRET["TICKET_SIGNING_KEY"]).encode()


def signature_for(reference: str, version: int, issued_on: str) -> str:
    msg = f"AGREEMENT:{reference}:{version}:{issued_on}".encode()
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()[:20].upper()


def verify_url(agreement: OrderAgreement) -> str:
    base = settings.NEBRET["PUBLIC_BASE_URL"].rstrip("/")
    return f"{base}/agreements/verify/{agreement.order.reference}/{agreement.version}/{agreement.signature}/"


def check(reference: str, version: int, signature: str) -> OrderAgreement | None:
    a = OrderAgreement.objects.select_related("order__company").filter(order__reference=reference.upper(),
                                                                       version=version).first()
    if not a:
        return None
    expected = signature_for(a.order.reference, a.version, timezone.localtime(a.issued_at).date().isoformat())
    if not (hmac.compare_digest(expected, signature.upper()) and hmac.compare_digest(a.signature, signature.upper())):
        return None
    return a


def confirmed_payments(order: Order) -> list[dict]:
    rows = []
    for p in order.proofs.filter(status=BankTransferProof.Status.CONFIRMED).order_by("transfer_datetime"):
        rows.append({"bank": p.get_bank_display() if hasattr(p, "get_bank_display") else p.bank,
                     "channel": p.bank, "reference": p.normalised_reference,
                     "amount": str(p.partner_confirmed_amount or p.amount_verified),
                     "at": timezone.localtime(p.partner_confirmed_datetime or p.transfer_datetime).isoformat()})
    return rows


def schedule_issue(order: Order):
    pk = order.pk
    transaction.on_commit(lambda: issue_safely(pk))


def issue_safely(order_id: int):
    try:
        issue(Order.objects.get(pk=order_id))
    except Exception:  # noqa: BLE001 – the scheduler retries via issue_missing_agreements()
        log.exception("Agreement for order %s could not be issued", order_id)


def needs_new_version(order: Order) -> bool:
    if order.status not in (*Order.LIVE_STATUSES, Order.Status.COMPLETED) or not order.is_bank_transfer:
        return False
    latest = order.agreements.order_by("-version").first()
    return latest is None or len(latest.payments_snapshot) != len(confirmed_payments(order))


@transaction.atomic
def issue(order: Order) -> OrderAgreement | None:
    from apps.core import private_files

    order = Order.objects.select_for_update(of=("self",)).select_related("company", "listing", "unit", "user").get(
        pk=order.pk)
    if not needs_new_version(order):
        return None
    latest = order.agreements.order_by("-version").first()
    version = (latest.version + 1) if latest else 1
    now = timezone.now()
    payments = confirmed_payments(order)
    sig = signature_for(order.reference, version, timezone.localtime(now).date().isoformat())
    agreement = OrderAgreement(order=order, version=version, signature=sig, payments_snapshot=payments)
    agreement.issued_at = now
    pdf = render_pdf(order, agreement, payments)
    agreement.file = private_files.store(pdf, "application/pdf", folder=f"agreements/{order.reference}",
                                         name=f"{order.reference}-agreement-v{version}.pdf")
    agreement.save()
    OrderAgreement.objects.filter(pk=agreement.pk).update(issued_at=now)
    if latest:
        OrderAgreement.objects.filter(order=order, superseded_at__isnull=True).exclude(pk=agreement.pk).update(
            superseded_at=now)
    log_event(order, OrderEvent.Kind.AGREEMENT_ISSUED, f"{settings.NEBRET['AGREEMENT_TITLE']} version {version} "
                                                       f"issued ({len(payments)} confirmed payment(s)).")
    from apps.notifications.models import Notification
    from apps.notifications.services import notify

    transaction.on_commit(lambda: notify(order.user, Notification.Kind.ORDER, "Your agreement is ready",
                                         f"{settings.NEBRET['AGREEMENT_TITLE']} for {order.reference} (version "
                                         f"{version}) can be downloaded from the order.", {"order": order.reference}))
    return agreement


def issue_missing_agreements() -> int:
    n = 0
    for order in Order.objects.filter(payment_method=Order.PaymentMethod.BANK_TRANSFER,
                                      status__in=[*Order.LIVE_STATUSES, Order.Status.COMPLETED],
                                      proofs__status=BankTransferProof.Status.CONFIRMED).distinct():
        if needs_new_version(order):
            issue_safely(order.pk)
            n += 1
    return n


def _logo(url: str):
    from reportlab.lib.utils import ImageReader

    if not url.startswith("https://"):
        return None
    try:
        resp = requests.get(url, timeout=8)
        resp.raise_for_status()
        return ImageReader(io.BytesIO(resp.content))
    except Exception:  # noqa: BLE001 – a missing logo never blocks the agreement
        return None


def render_pdf(order: Order, agreement: OrderAgreement, payments: list[dict]) -> bytes:
    import qrcode
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=f"{settings.NEBRET['AGREEMENT_TITLE']} {order.reference}")
    ss = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=ss["Heading1"], fontSize=16, textColor=colors.HexColor("#0F382B"))
    h2 = ParagraphStyle("h2", parent=ss["Heading3"], textColor=colors.HexColor("#0F382B"), spaceBefore=8)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=body, fontSize=8, textColor=colors.HexColor("#5B6F66"))
    grid = TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D6E0DA")),
                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7F0EC")),
                       ("FONTSIZE", (0, 0), (-1, -1), 8.5), ("VALIGN", (0, 0), (-1, -1), "TOP")])
    kv = TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#5B6F66")),
                     ("VALIGN", (0, 0), (-1, -1), "TOP")])
    story = []
    company = order.company
    logo = _logo(company.logo_url) if company and company.logo_url else None
    title = Paragraph(f"{settings.NEBRET['AGREEMENT_TITLE']}<br/><font size=9 color='#5B6F66'>Order "
                      f"{order.reference} · version {agreement.version} · issued "
                      f"{timezone.localtime(agreement.issued_at):%d %B %Y}</font>", h)
    story.append(Table([[Image(logo, 22 * mm, 22 * mm) if logo else "", title]], colWidths=[26 * mm, None],
                       style=[("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(Spacer(1, 4 * mm))

    seller = [["Seller", company.name if company else "Nebret"]]
    if company:
        seller += [["Trade licence", company.license_number or "—"], ["TIN", company.tin or "—"],
                   ["Address", ", ".join(x for x in [company.address, company.city] if x) or "—"]]
    buyer = [["Buyer", order.buyer_name or order.user.full_name], ["ID number", order.buyer_id_number or "—"]]
    story.append(Paragraph("Parties", h2))
    story.append(Table([[Table(seller, style=kv), Table(buyer, style=kv)]], colWidths=["50%", "50%"],
                       style=[("VALIGN", (0, 0), (-1, -1), "TOP")]))

    story.append(Paragraph("Item", h2))
    unit = order.unit
    story.append(Table([["Listing", order.listing.title], ["Unit", (unit.label or unit.code) if unit else "—"],
                        ["Unit code", unit.code if unit else "—"]], style=kv))

    story.append(Paragraph("Price and payment plan", h2))
    lines = [["Item", "Amount (ETB)"]] + [[ln["label"], fmt(ln["amount"])]
                                         for ln in (order.price_breakdown or {}).get("lines", [])]
    if len(lines) == 1:
        lines += [["Price", fmt(order.unit_price)], ["Total", fmt(order.total_amount)]]
    story.append(Table(lines, colWidths=[None, 40 * mm], style=grid))
    story.append(Paragraph(f"Plan: {order.get_plan_display()} · paid so far {fmt(order.amount_paid)} ETB · balance "
                           f"{fmt(order.balance_due)} ETB · payment code {order.payment_code}", body))
    if order.installments.exists():
        story.append(Paragraph("Installment schedule", h2))
        rows = [["#", "Due date", "Amount", "Paid", "Status"]] + [
            [str(i.number), f"{i.due_date:%d %b %Y}", fmt(i.amount), fmt(i.paid_amount), i.get_status_display()]
            for i in order.installments.order_by("number")]
        story.append(Table(rows, style=grid))

    story.append(Paragraph("Confirmed payments", h2))
    prow = [["Date / time", "Bank", "Reference", "Amount (ETB)"]] + [
        [p["at"][:16].replace("T", " "), p["channel"], p["reference"], fmt(p["amount"])] for p in payments]
    story.append(Table(prow if payments else prow + [["—", "—", "—", "—"]], style=grid))
    acc = order.pay_to_account or {}
    story.append(Paragraph(f"Paid into the seller's account: {acc.get('bank_name', '')} "
                           f"{('•' * 4) + str(acc.get('account_number', ''))[-4:]} ({acc.get('account_name', '')}).",
                           small))

    story.append(Paragraph("Terms", h2))
    for i, clause in enumerate(LEGAL_CLAUSES, start=1):
        story.append(Paragraph(f"{i}. {clause}", body))

    qr = qrcode.QRCode(box_size=4, border=1)
    qr.add_data(verify_url(agreement))
    img = qr.make_image(fill_color="black", back_color="white")
    qbuf = io.BytesIO()
    img.save(qbuf, format="PNG")
    qbuf.seek(0)
    story.append(Spacer(1, 6 * mm))
    story.append(Table([[Image(qbuf, 28 * mm, 28 * mm),
                         Paragraph(f"Scan to check this document is genuine.<br/>Signature {agreement.signature}<br/>"
                                   f"{verify_url(agreement)}", small)]], colWidths=[32 * mm, None],
                       style=[("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    doc.build(story)
    return buf.getvalue()


def agreement_rows(order: Order, request=None) -> list[dict]:
    from apps.core import private_files

    return [{"version": a.version, "issued_at": a.issued_at, "superseded": a.superseded_at is not None,
             "download_url": private_files.signed_url(a.file, request), "signature": a.signature,
             "title": settings.NEBRET["AGREEMENT_TITLE"]} for a in order.agreements.order_by("-version")]
