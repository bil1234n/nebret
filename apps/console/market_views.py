"""
Staff console – marketplace operations: orders (work queues, filters, CSV), the order page (office payments,
installments, handover, cancellations, refunds, notes, messages), installments receivable, the handover desk,
listings & stock, and customer inquiries.
"""
from __future__ import annotations

from datetime import timedelta

from django import forms as dj_forms
from django.conf import settings
from django.contrib import messages
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.agencies.forms import ListingForm, SaleForm, UnitsForm
from apps.equb.product import listing_overview
from apps.agencies.models import Agency
from apps.market.models import Category, Inquiry, Installment, Listing, ListingUnit, Order
from apps.market.services import handover as handover_svc
from apps.market.services.agreement import agreement_rows
from apps.market.services import operations as ops
from apps.market.services import orders as order_svc
from apps.payments.models import PaymentTransaction

from .equb_views import _csv, _date, _do
from .views import page, pct, staff_required

QUEUES = [
    ("all", "All orders"),
    ("pending", "Awaiting first payment"),
    ("installments_late", "Installments overdue"),
    ("handover", "Ready for handover"),
    ("refund_due", "Refunds due"),
]
EVENT_TONE = {"CONFIRMED": "green", "PAID_IN_FULL": "green", "HANDED_OVER": "green", "PAYMENT": "gold",
              "INSTALLMENT": "gold", "CREATED": "grey", "CANCELLED": "red", "EXPIRED": "red", "REFUNDED": "red",
              "NOTE": "grey", "MESSAGE": "grey"}


def _queue(qs, key: str):
    now = timezone.now()
    if key == "pending":
        return qs.filter(status=Order.Status.PENDING_PAYMENT, expires_at__gt=now).order_by("expires_at")
    if key == "installments_late":
        return qs.filter(status=Order.Status.INSTALLMENTS, installments__status="LATE").distinct().order_by("-updated_at")
    if key == "handover":
        return qs.filter(status=Order.Status.PAID).order_by("handover_due")
    if key == "refund_due":
        return qs.filter(status__in=[Order.Status.CANCELLED, Order.Status.DEFAULTED],
                         amount_paid__gt=F("refunded_amount")).order_by("-cancelled_at")
    return qs.order_by("-created_at")


# --------------------------------------------------------------------------- orders
@staff_required
def orders(request):
    base = Order.objects.select_related("user", "listing__category", "unit", "company")
    q, status, plan = request.GET.get("q", "").strip(), request.GET.get("status", ""), request.GET.get("plan", "")
    company, group = request.GET.get("company", ""), request.GET.get("group", "")
    start, end = _date(request.GET.get("from", "")), _date(request.GET.get("to", ""))
    queue = request.GET.get("queue", "all")
    if queue not in dict(QUEUES):
        queue = "all"
    qs = base
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(user__phone__icontains=q) | Q(user__full_name__icontains=q)
                       | Q(contact_phone__icontains=q) | Q(listing__title__icontains=q) | Q(unit__code__iexact=q)
                       | Q(buyer_id_number__iexact=q))
    if status:
        qs = qs.filter(status=status)
    if plan:
        qs = qs.filter(plan=plan)
    if company:
        qs = qs.filter(company_id=company) if company.isdigit() else qs.filter(company__isnull=True)
    if group:
        qs = qs.filter(listing__category__group=group)
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    qs = _queue(qs, queue)

    if request.GET.get("export") == "csv":
        rows = ([o.reference, timezone.localtime(o.created_at).strftime("%Y-%m-%d %H:%M"), o.get_status_display(),
                 o.get_plan_display(), o.listing.title, o.unit.code if o.unit_id else "", o.listing.category.name,
                 o.company.name if o.company_id else "Nebret", o.user.full_name, o.user.phone, o.buyer_name,
                 o.buyer_id_number, o.get_funding_source_display(), f"{o.total_amount:.2f}", f"{o.amount_paid:.2f}",
                 f"{o.balance_due:.2f}", f"{o.refunded_amount:.2f}"] for o in qs.iterator(chunk_size=500))
        return _csv(f"nebret-orders-{timezone.localdate()}.csv",
                    ["Reference", "Created", "Status", "Plan", "Listing", "Unit", "Category", "Seller", "Customer",
                     "Customer phone", "Buyer on contract", "Buyer ID", "Funding", "Total (ETB)", "Paid (ETB)",
                     "Balance (ETB)", "Refunded (ETB)"], rows)

    totals = qs.aggregate(n=Count("id", distinct=True), value=Sum("total_amount"), paid=Sum("amount_paid"))
    counts = {k: _queue(Order.objects.all(), k).count() for k, _ in QUEUES if k != "all"}
    return render(request, "console/orders.html", {
        "nav": "orders", "page": page(request, qs), "q": q, "status": status, "plan": plan, "totals": totals,
        "statuses": Order.Status.choices, "plans": Order.Plan.choices, "queue": queue, "queues": QUEUES,
        "counts": counts, "companies": Agency.objects.order_by("name"), "company": company, "group": group,
        "groups": Category.Group.choices, "from": request.GET.get("from", ""), "to": request.GET.get("to", ""),
    })


@staff_required
def order_detail(request, reference):
    o = get_object_or_404(Order.objects.select_related("user", "listing__category", "unit", "company"),
                          reference=reference)
    here = reverse("console:order", args=[reference])
    if request.method == "POST":
        a, p = request.POST.get("action"), request.POST
        if a == "office_payment":
            _do(request, lambda: ops.record_office_payment(o, request.user, amount=p.get("amount") or 0,
                                                           method=p.get("method", ""), reference=p.get("reference", ""),
                                                           note=p.get("note", ""), bank=p.get("bank", ""),
                                                           paid_to=p.get("paid_to") or None), "Payment recorded.")
        elif a == "handover":
            _do(request, lambda: order_svc.complete_handover(o, staff=request.user, note=p.get("note", "")),
                "Handover recorded – the customer has been notified.")
        elif a == "cancel":
            _do(request, lambda: order_svc.cancel_order(o, p.get("reason") or "Cancelled by Nebret",
                                                        staff=request.user), "Order cancelled – the unit is back on sale.")
        elif a == "refund":
            _do(request, lambda: ops.refund_order(o, request.user, amount=p.get("amount") or 0,
                                                  method=p.get("method", "Bank transfer"),
                                                  reference=p.get("reference", ""), note=p.get("note", "")),
                "Refund recorded – the customer has been notified.")
        elif a == "waive_fee":
            inst = get_object_or_404(Installment, pk=p.get("installment"), order=o)
            _do(request, lambda: ops.waive_installment_fee(inst, request.user, p.get("reason", "")), "Late fee waived.")
        elif a == "note":
            _do(request, lambda: ops.add_note(o, request.user, p.get("note", "")), "Note added.")
        elif a == "message":
            _do(request, lambda: ops.message_customer(o, request.user, p.get("title", ""), p.get("body", "")),
                "Message sent to the customer.")
        return redirect(here)
    payments = PaymentTransaction.objects.filter(purpose__in=["ORDER", "INSTALLMENT"], object_id=o.pk,
                                                 content_type__model="order").order_by("-created_at")
    events = list(o.events.select_related("by")[:60])
    for e in events:
        e.tone = EVENT_TONE.get(e.kind, "grey")
    return render(request, "console/order_detail.html", {
        "nav": "orders", "o": o, "payments": payments, "events": events, "installments": o.installments.all(),
        "methods": ops.OFFICE_METHODS, "policy": order_svc.cancellation_policy(o),
        "pass": handover_svc.pass_payload(o) if o.status in (Order.Status.PAID, Order.Status.COMPLETED) else "",
        "refund_left": o.amount_paid - o.refunded_amount,
        "refund_methods": ["Bank transfer", "Cash", "telebirr", "Chapa", "Equb credit"],
        "proofs": o.proofs.all(), "agreements": agreement_rows(o, request),
        "office_on": settings.NEBRET.get("MARKET_OFFICE_PAYMENTS"),
    })


@staff_required
def installments(request):
    """Installments receivable: overdue first, then what falls due in the next 30 days."""
    tab = request.GET.get("tab", "late")
    today = timezone.localdate()
    base = Installment.objects.filter(order__status=Order.Status.INSTALLMENTS).select_related(
        "order__user", "order__listing", "order__company")
    if tab == "late":
        qs = base.filter(status=Installment.Status.LATE).order_by("due_date")
    elif tab == "upcoming":
        qs = base.filter(status=Installment.Status.PENDING, due_date__lte=today + timedelta(days=30)).order_by("due_date")
    else:
        tab = "paid"
        qs = Installment.objects.filter(status=Installment.Status.PAID).select_related(
            "order__user", "order__listing").order_by("-paid_at")
    open_ = base.exclude(status__in=[Installment.Status.PAID, Installment.Status.WAIVED])
    summary = {
        "receivable": sum((i.amount_due for i in open_), 0),
        "late_count": base.filter(status=Installment.Status.LATE).count(),
        "late_amount": sum((i.amount_due for i in base.filter(status=Installment.Status.LATE)), 0),
        "orders": Order.objects.filter(status=Order.Status.INSTALLMENTS).count(),
    }
    return render(request, "console/installments.html", {"nav": "installments", "page": page(request, qs), "tab": tab,
                                                         "summary": summary, "today": today})


@staff_required
def handover_desk(request):
    found, payload = None, request.POST.get("payload", "").strip() if request.method == "POST" else ""
    if payload:
        found = handover_svc.verify_pass(payload)
        if not found:
            messages.error(request, "That pass is not valid.")
    due = Order.objects.filter(status=Order.Status.PAID).select_related("user", "listing", "unit", "company") \
        .order_by("handover_due")[:50]
    return render(request, "console/handover.html", {"nav": "handover", "found": found, "payload": payload, "due": due,
                                                     "today": timezone.localdate()})


@staff_required
def sell(request):
    """Place an order for a customer at the office (staff search the customer by phone)."""
    from apps.accounts.models import User, normalize_phone

    phone = request.GET.get("phone") or request.POST.get("phone") or ""
    customer = User.objects.filter(phone=normalize_phone(phone), is_staff=False).first() if phone else None
    form = SaleForm(request.POST or None, initial={"listing": request.GET.get("listing"),
                                                  "buyer_name": customer.full_name if customer else ""})
    if request.method == "POST" and customer and form.is_valid():
        d = form.cleaned_data
        o = _do(request, lambda: order_svc.create_order(
            user=customer, listing=d["listing"], plan=d["plan"], months=d.get("months") or 0,
            down_payment=d.get("down_payment"), delivery_method=d["delivery_method"],
            delivery_address=d.get("delivery_address", ""), buyer_name=d.get("buyer_name", ""),
            buyer_id_number=d.get("buyer_id_number", ""), by=request.user))
        if o:
            messages.success(request, f"Order {o.reference} created – {o.first_payment:,.2f} ETB is due now.")
            return redirect("console:order", reference=o.reference)
    elif request.method == "POST" and not customer:
        messages.error(request, "No customer was found with that phone number.")
    return render(request, "console/sell.html", {"nav": "orders", "form": form, "customer": customer, "phone": phone})


# --------------------------------------------------------------------------- listings & stock
@staff_required
def listings(request):
    q, status = request.GET.get("q", "").strip(), request.GET.get("status", "")
    company, category = request.GET.get("company", ""), request.GET.get("category", "")
    qs = Listing.objects.select_related("category", "company").annotate(
        available=Count("units", filter=Q(units__status=ListingUnit.Status.AVAILABLE), distinct=True),
        sold=Count("units", filter=Q(units__status=ListingUnit.Status.SOLD), distinct=True),
        n_orders=Count("orders", filter=Q(orders__status__in=Order.LIVE_STATUSES), distinct=True))
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(brand__icontains=q) | Q(model_name__icontains=q) | Q(area__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if company:
        qs = qs.filter(company_id=company) if company.isdigit() else qs.filter(company__isnull=True)
    if category:
        qs = qs.filter(category__slug=category)
    by_group = (Listing.objects.filter(status=Listing.Status.ACTIVE).values("category__group")
                .annotate(n=Count("id")).order_by("-n"))
    labels = dict(Category.Group.choices)
    charts = {"groups": {"type": "doughnut", "labels": [labels.get(r["category__group"], "") for r in by_group],
                         "datasets": [{"label": "Live listings", "data": [r["n"] for r in by_group]}]}}
    return render(request, "console/listings.html", {
        "nav": "listings", "page": page(request, qs.order_by("-created_at")), "q": q, "status": status,
        "statuses": Listing.Status.choices, "companies": Agency.objects.order_by("name"), "company": company,
        "categories": Category.objects.order_by("group", "name"), "category": category, "charts": charts,
        "stock": {"available": ListingUnit.objects.filter(status="AVAILABLE").count(),
                  "reserved": ListingUnit.objects.filter(status__in=["HELD", "RESERVED"]).count(),
                  "sold": ListingUnit.objects.filter(status="SOLD").count()},
    })


@staff_required
def listing_form(request, pk=None):
    obj = get_object_or_404(Listing, pk=pk) if pk else None
    form = ListingForm(request.POST or None, request.FILES or None, instance=obj, allow_new_category=True)
    form.fields["company"] = dj_forms.ModelChoiceField(
        queryset=Agency.objects.order_by("name"), required=False, initial=obj.company_id if obj else None,
        empty_label="Nebret (sold directly)", label="Seller")
    form.sections = [("Seller", "", ["company"])] + form.sections
    if request.method == "POST" and form.is_valid():
        listing = form.save(commit=False)
        listing.company = form.cleaned_data.get("company")
        if not obj:
            listing.created_by = request.user
        listing.save()
        form.save_gallery(listing)
        if not obj and form.cleaned_data.get("initial_units"):
            listing.add_units(form.cleaned_data["initial_units"])
        messages.success(request, f"“{listing.title}” saved.")
        return redirect("console:listing", pk=listing.pk)
    from .manage_views import _render

    return _render(request, nav="listings", title=f"Edit {obj.title}" if obj else "New listing", crumbs="Listings",
                   form=form, obj=obj,
                   cancel=reverse("console:listing", args=[obj.pk]) if obj else reverse("console:listings"),
                   delete_url=reverse("console:listing_delete", args=[obj.pk]) if obj and not obj.orders.exists() else None)


@staff_required
@require_POST
def listing_delete(request, pk):
    obj = get_object_or_404(Listing, pk=pk)
    if obj.orders.exists():
        messages.error(request, "This listing has orders; archive it instead.")
        return redirect("console:listing", pk=pk)
    obj.delete()
    messages.success(request, "Listing deleted.")
    return redirect("console:listings")


@staff_required
def listing_detail(request, pk):
    listing = get_object_or_404(Listing.objects.select_related("category", "company"), pk=pk)
    here = reverse("console:listing", args=[pk])
    units_form = UnitsForm(request.POST if request.POST.get("action") == "add_units" else None)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_units" and units_form.is_valid():
            listing.add_units(units_form.cleaned_data["count"], prefix=units_form.cleaned_data["prefix"])
            messages.success(request, f"{units_form.cleaned_data['count']} unit(s) added.")
        elif action in ("block", "unblock", "rename"):
            unit = get_object_or_404(ListingUnit, pk=request.POST.get("unit"), listing=listing)
            if action == "rename":
                unit.label = request.POST.get("label", "")[:120]
                unit.save(update_fields=["label"])
            elif unit.status in (ListingUnit.Status.AVAILABLE, ListingUnit.Status.BLOCKED):
                unit.status = ListingUnit.Status.BLOCKED if action == "block" else ListingUnit.Status.AVAILABLE
                unit.save(update_fields=["status"])
                listing.refresh_stock_status()
        elif action in ("publish", "unpublish", "archive", "feature"):
            if action == "feature":
                listing.is_featured = not listing.is_featured
            else:
                listing.status = {"publish": Listing.Status.ACTIVE, "unpublish": Listing.Status.DRAFT,
                                  "archive": Listing.Status.ARCHIVED}[action]
            listing.save(update_fields=["status", "is_featured", "updated_at"])
            listing.refresh_stock_status()
        return redirect(here)
    units = listing.units.select_related("held_by")
    stock = {s: units.filter(status=s).count() for s in ListingUnit.Status.values}
    revenue = listing.orders.exclude(status__in=["CANCELLED", "EXPIRED", "REFUNDED"]).aggregate(
        s=Sum("amount_paid"))["s"] or 0
    return render(request, "console/listing_detail.html", {
        "nav": "listings", "l": listing, "units": units, "units_form": units_form, "stock": stock, "revenue": revenue,
        "sold_pct": pct(stock.get("SOLD", 0), len(units)), "equbs": listing_overview(listing),
        "orders": listing.orders.select_related("user", "unit").order_by("-created_at")[:30],
        "inquiries": listing.inquiries.select_related("user")[:10]})


# --------------------------------------------------------------------------- inquiries
@staff_required
def inquiries(request):
    status = request.GET.get("status", "OPEN")
    qs = Inquiry.objects.select_related("listing__company", "user")
    if request.method == "POST":
        inq = get_object_or_404(Inquiry, pk=request.POST.get("inquiry"))
        reply = request.POST.get("reply", "").strip()
        if reply:
            from apps.notifications.models import Notification
            from apps.notifications.services import notify

            inq.reply, inq.status = reply[:2000], Inquiry.Status.ANSWERED
            inq.replied_by, inq.replied_at = request.user, timezone.now()
            inq.save()
            notify(inq.user, Notification.Kind.ORDER, "Nebret replied",
                   f"About {inq.listing.title}: {reply[:300]}", {"listing": inq.listing.slug})
            messages.success(request, "Reply sent to the customer.")
        elif request.POST.get("action") == "close":
            inq.status = Inquiry.Status.CLOSED
            inq.save(update_fields=["status", "updated_at"])
        return redirect(f"{reverse('console:inquiries')}?status={status}")
    if status:
        qs = qs.filter(status=status)
    return render(request, "console/inquiries.html", {"nav": "inquiries", "page": page(request, qs), "status": status,
                                                     "statuses": Inquiry.Status.choices})
