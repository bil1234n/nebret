"""Public pages for the marketplace (no sign-in)."""
from django.shortcuts import render
from django.utils import timezone

from .services import agreement as agreement_svc


def verify_agreement(request, reference: str, version: int, signature: str):
    """Scanned from an agreement's QR code. Shows only valid / invalid, order reference, company and issue date."""
    a = agreement_svc.check(reference, version, signature)
    ctx = {"valid": a is not None, "reference": reference.upper()}
    if a:
        ctx.update(company=a.order.company.name if a.order.company_id else "Nebret", version=a.version,
                   issued=timezone.localtime(a.issued_at), superseded=a.superseded_at is not None)
    return render(request, "market/agreement_verify.html", ctx, status=200 if a else 404)
