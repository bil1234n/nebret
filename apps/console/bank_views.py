"""
Staff console – marketplace bank transfers and trust & safety:

* receipts (all proofs, filter by status / company / age) and one receipt (ShegerPay result, receipt viewer, manual
  review, confirm or reject for the seller with a note + evidence, settle disputes);
* alerts (duplicate references, repeated rejections, verifier outages, disputes, overdue confirmations / refunds,
  contact details in chat);
* partner bank accounts waiting for approval;
* commission receivables (invoice, mark collected, write off);
* product Equbs created from a listing;
* chat: conversation search, flagged / reported queue, moderation actions, response-time stats per company.
"""
from __future__ import annotations

from decimal import Decimal

from datetime import timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.agencies import bank_accounts as ba
from apps.agencies.models import Agency, AgencyLedgerEntry, PartnerBankAccount
from apps.chat import services as chat
from apps.chat import web as chat_web
from apps.chat.models import Conversation, Message, Report, Restriction, Violation
from apps.core import private_files
from apps.core.exceptions import BusinessRuleError
from apps.market.models import BankTransferProof, Listing
from apps.market.services import bank_transfer
from apps.payments.models import FraudAlert

from .equb_views import _do
from .views import page, staff_required

P = BankTransferProof.Status
Payout = AgencyLedgerEntry.Payout


# =========================================================================== receipts
@staff_required
def proofs(request):
    qs = BankTransferProof.objects.select_related("order__user", "order__company").order_by("-created_at")
    status, company, age = request.GET.get("status", ""), request.GET.get("company", ""), request.GET.get("age", "")
    if status == "manual":
        qs = qs.filter(requires_manual_review=True, status__in=[P.SUBMITTED, P.VERIFYING])
    elif status:
        qs = qs.filter(status=status)
    if company:
        qs = qs.filter(order__company_id=company)
    if age.isdigit():
        qs = qs.filter(created_at__lte=timezone.now() - timedelta(days=int(age)))
    if request.GET.get("q"):
        q = request.GET["q"].strip()
        qs = qs.filter(Q(order__reference__iexact=q) | Q(normalised_reference__icontains=q.upper())
                       | Q(payer_name__icontains=q))
    counts = dict(BankTransferProof.objects.values_list("status").annotate(n=Count("id")))
    counts["manual"] = BankTransferProof.objects.filter(requires_manual_review=True,
                                                        status__in=[P.SUBMITTED, P.VERIFYING]).count()
    return render(request, "console/proofs.html", {
        "nav": "proofs", "page": page(request, qs), "status": status, "statuses": P.choices, "counts": counts,
        "companies": Agency.objects.order_by("name"), "company": company, "age": age, "q": request.GET.get("q", ""),
        "open_alerts": FraudAlert.objects.filter(resolved_at__isnull=True).count()})


@staff_required
def proof(request, pk):
    p = get_object_or_404(BankTransferProof.objects.select_related("order__user", "order__company",
                                                                  "partner_confirmed_by", "second_confirmer"), pk=pk)
    here = reverse("console:proof", args=[pk])
    if request.method == "GET" and p.status in (P.SUBMITTED, P.VERIFYING) and not p.requires_manual_review \
            and (p.next_check_at is None or p.next_check_at <= timezone.now()):
        p = bank_transfer.verify_proof(p)
    if request.method == "POST":
        a, d = request.POST.get("action"), request.POST
        if a in ("approve", "decline"):
            _do(request, lambda: bank_transfer.manual_review(p, request.user, approve=a == "approve",
                                                             note=d.get("note", ""), amount=d.get("amount") or None),
                "Receipt reviewed.")
        elif a == "confirm":
            if _do(request, lambda: bank_transfer.confirm_proof(
                    p, request.user, amount=d.get("amount") or 0, payer_name=d.get("payer_name", ""),
                    credited_at=d.get("credited_at"), statement_ref=d.get("statement_ref", ""),
                    note=d.get("note", ""), evidence=request.FILES.get("evidence"))):
                p.refresh_from_db()
                messages.success(request, f"Receipt is now: {p.get_status_display()}.")
        elif a == "reject":
            _do(request, lambda: bank_transfer.reject_proof(p, request.user, d.get("reason", "")), "Receipt rejected.")
        elif a == "recheck":
            BankTransferProof.objects.filter(pk=p.pk, status__in=[P.SUBMITTED, P.VERIFYING]).update(
                next_check_at=timezone.now(), requires_manual_review=False)
            p = bank_transfer.verify_proof(BankTransferProof.objects.get(pk=p.pk))
            r = p.shegerpay_response or {}
            messages.info(request, f"Checked with the bank now: {p.get_status_display()}"
                                   + (f" – {r.get('reason')}" if r.get("reason") else "") + ".")
        return redirect(here)
    return render(request, "console/proof.html", {
        "nav": "proofs", "p": p, "o": p.order, "receipt_url": private_files.signed_url(p.file, request),
        "evidence_url": private_files.signed_url(p.on_behalf_evidence, request) if p.on_behalf_evidence else "",
        "alerts": FraudAlert.objects.filter(Q(proof_id=p.pk) | Q(order_reference=p.order.reference))[:20],
        "others": p.order.proofs.exclude(pk=p.pk), "row": bank_transfer.proof_row(p, request, for_staff=True)})


# =========================================================================== alerts
@staff_required
def alerts(request):
    if request.method == "POST":
        alert = get_object_or_404(FraudAlert, pk=request.POST.get("alert") or 0)
        alert.resolved_at, alert.resolved_by = timezone.now(), request.user
        alert.resolution_note = request.POST.get("note", "")[:255]
        alert.save(update_fields=["resolved_at", "resolved_by", "resolution_note", "updated_at"])
        messages.success(request, "Alert resolved.")
        return redirect(request.get_full_path())
    kind, show = request.GET.get("kind", ""), request.GET.get("show", "open")
    qs = FraudAlert.objects.select_related("agency", "user", "resolved_by").order_by("-created_at")
    if kind:
        qs = qs.filter(kind=kind)
    if show == "open":
        qs = qs.filter(resolved_at__isnull=True)
    counts = dict(FraudAlert.objects.filter(resolved_at__isnull=True).values_list("kind").annotate(n=Count("id")))
    return render(request, "console/alerts.html", {"nav": "alerts", "page": page(request, qs), "kind": kind,
                                                   "kinds": FraudAlert.Kind.choices, "show": show, "counts": counts})


# =========================================================================== bank accounts
@staff_required
def bank_accounts(request):
    if request.method == "POST":
        acc = get_object_or_404(PartnerBankAccount, pk=request.POST.get("account") or 0)
        a = request.POST.get("action")
        if a == "approve":
            _do(request, lambda: (ba.make_usable_now(ba.approve_account(acc, request.user, request.POST.get("note", "")),
                                                      request.user, "Approved and put into use at once")
                                  if request.POST.get("now") else ba.approve_account(acc, request.user, request.POST.get("note", ""))),
                "Approved – in use now." if request.POST.get("now") else "Approved – usable after the waiting period.")
        elif a == "reject":
            _do(request, lambda: ba.reject_account(acc, request.user, request.POST.get("reason", "")), "Rejected.")
        elif a == "use_now":
            _do(request, lambda: ba.make_usable_now(acc, request.user, request.POST.get("note", "")),
                "Done – buyers pay into this account from now on.")
        elif a == "retire":
            _do(request, lambda: ba.retire_account(acc, request.user, request.POST.get("note", "")), "Switched off.")
        nxt = request.POST.get("next", "")
        return redirect(nxt if nxt.startswith("/console/") else request.get_full_path())
    status = request.GET.get("status", "PENDING")
    qs = PartnerBankAccount.objects.select_related("agency", "created_by", "verified_by", "replaces").prefetch_related("events")
    if status:
        qs = qs.filter(status=status)
    return render(request, "console/bank_accounts.html", {
        "nav": "bank_accounts", "page": page(request, qs.order_by("-created_at")), "status": status,
        "statuses": PartnerBankAccount.Status.choices,
        "pending": PartnerBankAccount.objects.filter(status=PartnerBankAccount.Status.PENDING).count()})


# =========================================================================== commission receivables
@staff_required
def commission(request):
    from apps.agencies import commission as cm
    from apps.agencies.models import CommissionPayment

    if request.method == "POST" and request.POST.get("payment"):
        cp = get_object_or_404(CommissionPayment, pk=request.POST["payment"])
        a = request.POST.get("action")
        if a == "recheck":
            CommissionPayment.objects.filter(pk=cp.pk, status__in=["SUBMITTED", "MANUAL"]).update(status="SUBMITTED")
            cp = cm.verify(CommissionPayment.objects.get(pk=cp.pk))
            messages.info(request, f"Checked with the bank: {cp.get_status_display()}"
                                   + (f" – {cp.reason}" if cp.reason else "") + ".")
        else:
            _do(request, lambda: cm.manual_decision(cp, request.user, approve=a == "approve",
                                                    note=request.POST.get("note", ""),
                                                    amount=request.POST.get("amount") or None),
                "Commission payment approved – commission marked collected." if a == "approve" else "Rejected.")
        return redirect(request.get_full_path())
    if request.method == "POST":
        ids = [int(x) for x in request.POST.getlist("entry") if x.isdigit()]
        a, ref = request.POST.get("action"), request.POST.get("reference", "")[:64]
        qs = AgencyLedgerEntry.objects.filter(pk__in=ids, direction=AgencyLedgerEntry.Direction.TO_NEBRET)
        now = timezone.now()
        if a == "invoice":
            n = qs.filter(payout_status=Payout.RECEIVABLE).update(payout_status=Payout.INVOICED, paid_reference=ref)
        elif a == "collected":
            n = qs.filter(payout_status__in=[Payout.RECEIVABLE, Payout.INVOICED]).update(
                payout_status=Payout.COLLECTED, paid_at=now, paid_by=request.user, paid_reference=ref)
        elif a == "write_off":
            n = qs.filter(payout_status__in=[Payout.RECEIVABLE, Payout.INVOICED]).update(
                payout_status=Payout.WRITTEN_OFF, paid_at=now, paid_by=request.user, paid_reference=ref)
        else:
            n = 0
        messages.success(request, f"{n} entr{'y' if n == 1 else 'ies'} updated.")
        return redirect(request.get_full_path())
    status, company = request.GET.get("status", "open"), request.GET.get("company", "")
    qs = AgencyLedgerEntry.objects.filter(direction=AgencyLedgerEntry.Direction.TO_NEBRET).select_related(
        "agency", "order", "customer")
    if status == "open":
        qs = qs.filter(payout_status__in=[Payout.RECEIVABLE, Payout.INVOICED])
    elif status:
        qs = qs.filter(payout_status=status)
    if company:
        qs = qs.filter(agency_id=company)
    by_company = (AgencyLedgerEntry.objects.filter(direction=AgencyLedgerEntry.Direction.TO_NEBRET,
                                                   payout_status__in=[Payout.RECEIVABLE, Payout.INVOICED])
                  .values("agency__id", "agency__name").annotate(due=Sum("commission"), n=Count("id"))
                  .order_by("-due"))
    return render(request, "console/commission.html", {
        "nav": "commission", "page": page(request, qs.order_by("-created_at")), "status": status,
        "company": company, "companies": Agency.objects.order_by("name"), "by_company": by_company,
        "total_due": sum((r["due"] or 0) for r in by_company),
        "payments": CommissionPayment.objects.select_related("agency", "submitted_by").order_by("-created_at")[:30]})


# =========================================================================== product Equbs
@staff_required
def listing_equb(request, pk):
    from apps.equb import product
    from apps.equb.models import DurationUnit, EqubGroup, EqubSettings

    listing = get_object_or_404(Listing.objects.select_related("company"), pk=pk)
    preview = None
    default_fee = EqubSettings.load().service_fee_percent
    available = listing.units.filter(status="AVAILABLE")
    price = max([u.price for u in available] or [listing.price])
    d = request.POST
    if request.method == "POST":
        start = parse_date(d.get("start_date", ""))
        try:
            if d.get("action") == "preview":
                fee = d.get("service_fee_percent") or default_fee
                try:
                    fee = Decimal(str(fee))
                except Exception as exc:  # noqa: BLE001
                    raise BusinessRuleError("Enter the service fee as a percentage, e.g. 2.") from exc
                args = dict(price=price, contribution=Decimal(d.get("contribution_amount") or 0),
                            duration_value=int(d.get("duration_value") or 0), fee_percent=fee)
                preview = product.maths(**args)
                preview["suggestions"] = product.suggestions(**args)
            else:
                if not start:
                    raise BusinessRuleError("Choose the start date.")
                group, m = product.create_product_equb(
                    listing, request.user, contribution_amount=d.get("contribution_amount") or 0,
                    duration_value=d.get("duration_value") or 0, duration_unit=d.get("duration_unit") or "DAYS",
                    members=d.get("members") or 0, start_date=start, rounds_drawn=d.get("rounds_drawn") or None,
                    name=d.get("name", ""), terms=d.get("terms", ""), draw_type=d.get("draw_type") or "AUTOMATIC",
                    service_fee_percent=d.get("service_fee_percent") or None)
                messages.success(request, f"“{group.name}” created – {m['explanation']}.")
                return redirect("console:equb", slug=group.slug)
        except (BusinessRuleError, ValueError) as exc:
            messages.error(request, str(getattr(exc, "detail", exc)))
    return render(request, "console/listing_equb.html", {
        "nav": "listings", "l": listing, "groups": EqubGroup.objects.filter(listing=listing).order_by("-created_at"),
        "units_available": listing.units.filter(status="AVAILABLE").count(), "preview": preview, "post": d,
        "units": DurationUnit.choices, "fee": default_fee, "price": price,
        "today": timezone.localdate()})


# =========================================================================== chat
@staff_required
def conversations(request):
    tab, q = request.GET.get("tab", "inbox"), request.GET.get("q", "").strip()
    last_role = Subquery(Message.objects.filter(conversation=OuterRef("pk"), deleted_at__isnull=True)
                         .order_by("-created_at", "-id").values("sender_role")[:1])
    qs = Conversation.objects.select_related("buyer", "agency", "listing", "order").annotate(last_role=last_role)
    staff_inbox = Q(kind=Conversation.Kind.SUPPORT) | Q(support_joined=True) | Q(agency__isnull=True)
    needs_reply = qs.filter(staff_inbox, status="OPEN", last_role__in=["BUYER", "AGENCY"])
    if tab == "inbox":
        qs = needs_reply
    elif tab == "flagged":
        qs = qs.filter(Q(messages__was_masked=True) | Q(reports__status=Report.Status.OPEN)).distinct()
    elif tab == "reported":
        qs = qs.filter(reports__status=Report.Status.OPEN).distinct()
    elif tab == "support":
        qs = qs.filter(kind=Conversation.Kind.SUPPORT)
    if q:
        qs = qs.filter(Q(buyer__full_name__icontains=q) | Q(buyer__phone__icontains=q) | Q(agency__name__icontains=q)
                       | Q(listing__title__icontains=q) | Q(order__reference__iexact=q) | Q(pk=q if q.isdigit() else 0))
    reply = ExpressionWrapper(F("first_agency_reply_at") - F("first_buyer_message_at"), output_field=DurationField())
    stats = (Conversation.objects.filter(agency__isnull=False, first_buyer_message_at__isnull=False)
             .values("agency__name").annotate(conversations=Count("id"), answered=Count("first_agency_reply_at"),
                                               avg_reply=Avg(reply)).order_by("agency__name"))
    top_violators = (Violation.objects.values("user__id", "user__full_name").annotate(n=Count("id"))
                     .order_by("-n")[:10])
    return render(request, "console/conversations.html", {
        "nav": "chat", "page": page(request, qs.order_by("-last_message_at", "-id")), "tab": tab, "q": q,
        "stats": stats, "violators": top_violators, "inbox_count": needs_reply.count(),
        "open_reports": Report.objects.filter(status=Report.Status.OPEN).count()})


@staff_required
def conversation(request, pk):
    conv = get_object_or_404(Conversation.objects.select_related("buyer", "agency", "listing", "order"), pk=pk)
    here = reverse("console:conversation", args=[pk])
    if request.method == "GET" and chat_web.is_ajax(request):
        if not chat.staff_may_read(conv):
            return HttpResponse("")
        return chat_web.render_messages(request, conv, staff_view=True)
    if request.method == "POST" and request.POST.get("action") == "send":
        nxt = request.POST.get("next", "")
        return chat_web.send_from_web(request, conv, nxt if nxt.startswith("/console/") else here)
    if request.method == "POST":
        a, d = request.POST.get("action"), request.POST
        if a == "join":
            chat.join(conv, request.user)
        elif a == "send":
            _do(request, lambda: chat.post_message(conv, request.user, d.get("text", "")), "Sent.")
        elif a in ("WARNED", "MUTED", "SUSPENDED"):
            from apps.accounts.models import User

            target = get_object_or_404(User, pk=d.get("user") or 0)
            _do(request, lambda: chat.restrict(target, request.user, a, days=int(d.get("days") or 0) or None,
                                               reason=d.get("reason", "")), "Done – the user has been notified.")
        elif a == "lift":
            r = get_object_or_404(Restriction, pk=d.get("restriction") or 0)
            chat.lift(r, request.user)
        elif a == "resolve_report":
            Report.objects.filter(pk=d.get("report") or 0).update(status=Report.Status.RESOLVED,
                                                                 resolved_by=request.user,
                                                                 resolution=d.get("note", "")[:255])
        elif a in ("close", "block", "reopen"):
            conv.status = {"close": "CLOSED", "block": "BLOCKED", "reopen": "OPEN"}[a]
            conv.save(update_fields=["status", "updated_at"])
        return redirect(here)
    readable = chat.staff_may_read(conv)
    msgs = chat_web.thread_rows(conv, request.user, request, staff_view=True) if readable else []
    if readable:
        chat.mark_read(conv, request.user)
    people = [conv.buyer] + ([s.user for s in conv.agency.staff.select_related("user")] if conv.agency_id else [])
    restrictions = Restriction.objects.filter(user__in=people, lifted_at__isnull=True).select_related("user")
    return render(request, "console/conversation.html", {
        "nav": "chat", "c": conv, "msgs": msgs, "readable": readable, "people": people, "staff_view": True,
        "can_send": conv.status == "OPEN", "poll_url": here, "send_url": here,
        "placeholder": "Reply as Nebret support… (Enter to send, Shift+Enter for a new line)",
        "restrictions": restrictions, "reports": conv.reports.select_related("reporter"),
        "violations": Violation.objects.filter(message__conversation=conv).select_related("user")[:50]})


@staff_required
def commission_receipt(request, pk):
    from apps.agencies.models import CommissionPayment

    cp = get_object_or_404(CommissionPayment, pk=pk)
    return redirect(private_files.signed_url(cp.file, request))
