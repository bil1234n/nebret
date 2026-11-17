"""
Company portal (/company/): a partner company's own workspace on Nebret.

Company staff sign in with the same login page as the console and only ever see their own company's data: their
listings and stock, orders and installments, handovers, customer inquiries, customers and their families, Equb places
and their earnings after Nebret's commission.
"""
from __future__ import annotations

from datetime import datetime, time
from functools import wraps

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.exceptions import BusinessRuleError
from apps.equb.models import EqubGroup, EqubMembership
from apps.market.models import Inquiry, Listing, ListingUnit, Order
from apps.market.services import handover as handover_svc
from apps.market.services import operations as ops
from apps.market.services import orders as order_svc
from apps.payments.models import PaymentTransaction

from . import forms as f
from . import services
from .models import AgencyCustomer, AgencyLedgerEntry, AgencyStaff, FamilyMember

PER_PAGE = 25


def agency_required(manage: bool = False):
    """Only signed-in staff of an active (or pending) company; ``manage`` = owners and managers only."""

    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(f"{reverse('console:login')}?next={request.get_full_path()}")
            st = services.staff_for(request.user)
            if st is None:
                messages.error(request, "This account is not linked to an active partner company.")
                return redirect("console:login")
            if manage and not st.can_manage:
                messages.error(request, "Only the company owner or a manager can do that.")
                return redirect("agency:dashboard")
            request.agency_staff, request.agency = st, st.agency
            return view(request, *args, **kwargs)
        return wrapper
    return deco


def _page(request, qs):
    return Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))


def _do(request, fn, ok: str = ""):
    try:
        out = fn()
    except BusinessRuleError as exc:
        messages.error(request, str(exc.detail))
        return None
    if ok:
        messages.success(request, ok)
    return out if out is not None else True


def _ctx(request, nav, **extra):
    return {"nav": nav, "agency": request.agency, "me": request.agency_staff, **extra}


def _payment_accounts(agency) -> dict:
    """The account buyers pay into and Nebret's account the company sends the commission to."""
    from apps.payments.banks import Bank, commission_account

    from .bank_accounts import account_for_order

    acc = account_for_order(agency)
    return {"pay_account": acc.snapshot() if acc else None, "commission_account": commission_account(),
            "bank_names": dict(Bank.choices)}


def _customer(request, pk) -> AgencyCustomer:
    return get_object_or_404(AgencyCustomer.objects.select_related("user"), pk=pk, agency=request.agency)


def _orders(request):
    return services.company_orders(request.agency).select_related("user", "listing", "unit")


def _listings(request):
    return Listing.objects.filter(company=request.agency)


# --------------------------------------------------------------------------- dashboard
@agency_required()
def dashboard(request):
    a = request.agency
    since = timezone.make_aware(datetime.combine(timezone.localdate().replace(day=1), time.min))
    orders = _orders(request)
    listings = _listings(request)
    return render(request, "agencies/dashboard.html", _ctx(
        request, "dashboard", **_payment_accounts(a), month=services.summary(a, since=since), total=services.summary(a),
        recent=a.ledger.select_related("customer", "order").order_by("-created_at")[:8],
        pending=orders.filter(status=Order.Status.PENDING_PAYMENT).order_by("expires_at")[:6],
        handovers=orders.filter(status=Order.Status.PAID).order_by("handover_due")[:6],
        counts={"listings": listings.filter(status=Listing.Status.ACTIVE).count(),
                "units": ListingUnit.objects.filter(listing__company=a, status=ListingUnit.Status.AVAILABLE).count(),
                "active_orders": orders.filter(status__in=Order.LIVE_STATUSES).count(),
                "installments": orders.filter(status=Order.Status.INSTALLMENTS).count(),
                "inquiries": Inquiry.objects.filter(listing__company=a, status=Inquiry.Status.OPEN).count(),
                "customers": a.customers.count()}))


# --------------------------------------------------------------------------- listings & stock
@agency_required()
def listings(request):
    q, status = request.GET.get("q", "").strip(), request.GET.get("status", "")
    qs = _listings(request).select_related("category").annotate(
        available=Count("units", filter=Q(units__status=ListingUnit.Status.AVAILABLE), distinct=True),
        sold=Count("units", filter=Q(units__status=ListingUnit.Status.SOLD), distinct=True),
        n_orders=Count("orders", filter=Q(orders__status__in=Order.LIVE_STATUSES), distinct=True)).order_by("-created_at")
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(brand__icontains=q) | Q(model_name__icontains=q))
    if status:
        qs = qs.filter(status=status)
    return render(request, "agencies/listings.html", _ctx(request, "listings", page=_page(request, qs), q=q,
                                                         status=status, statuses=Listing.Status.choices))


@agency_required(manage=True)
def listing_form(request, pk=None):
    obj = get_object_or_404(_listings(request), pk=pk) if pk else None
    form = f.ListingForm(request.POST or None, request.FILES or None, instance=obj, company_portal=True)
    if request.method == "POST" and form.is_valid():
        listing = form.save(commit=False)
        listing.company = request.agency
        if not obj:
            listing.created_by = request.user
            if request.agency.status != "ACTIVE":
                listing.status = Listing.Status.DRAFT
        from .bank_accounts import has_usable_account

        if listing.status == Listing.Status.ACTIVE and not has_usable_account(request.agency):
            listing.status = Listing.Status.DRAFT
            messages.warning(request, "Saved as a draft: add a bank account in the app and wait for Nebret's approval "
                                      "before products go live.")
        listing.save()
        form.save_gallery(listing)
        if not obj and form.cleaned_data.get("initial_units"):
            listing.add_units(form.cleaned_data["initial_units"])
        messages.success(request, f"“{listing.title}” saved.")
        return redirect("agency:listing", pk=listing.pk)
    return render(request, "agencies/form.html", _ctx(
        request, "listings", form=form, obj=obj, title=f"{'Edit' if obj else 'New'} listing",
        cancel=reverse("agency:listing", args=[obj.pk]) if obj else reverse("agency:listings")))


@agency_required()
def listing_detail(request, pk):
    listing = get_object_or_404(_listings(request).select_related("category"), pk=pk)
    here = reverse("agency:listing", args=[pk])
    units_form = f.UnitsForm(request.POST if request.POST.get("action") == "add_units" else None)
    if request.method == "POST":
        if not request.agency_staff.can_manage:
            messages.error(request, "Only the company owner or a manager can change stock.")
            return redirect(here)
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
            else:
                messages.error(request, "Units that are held, reserved or sold can't be changed.")
        elif action in ("publish", "unpublish"):
            from .bank_accounts import has_usable_account

            if action == "publish" and request.agency.status != "ACTIVE":
                messages.error(request, "Your company must be approved before products go live.")
            elif action == "publish" and not has_usable_account(request.agency):
                messages.error(request, "Add a bank account in the app and wait for Nebret's approval before products "
                                        "go live – buyers pay straight into it.")
            else:
                listing.status = Listing.Status.ACTIVE if action == "publish" else Listing.Status.DRAFT
                listing.save(update_fields=["status", "updated_at"])
                listing.refresh_stock_status()
        return redirect(here)
    units = listing.units.select_related("held_by").prefetch_related("orders")
    return render(request, "agencies/listing_detail.html", _ctx(
        request, "listings", l=listing, units=units, units_form=units_form,
        orders=listing.orders.select_related("user", "unit").order_by("-created_at")[:20],
        stock={s: units.filter(status=s).count() for s in ListingUnit.Status.values}))


# --------------------------------------------------------------------------- orders & handover
@agency_required()
def orders(request):
    status, plan, q = request.GET.get("status", ""), request.GET.get("plan", ""), request.GET.get("q", "").strip()
    qs = _orders(request)
    if status:
        qs = qs.filter(status=status)
    if plan:
        qs = qs.filter(plan=plan)
    if q:
        qs = qs.filter(Q(reference__iexact=q) | Q(user__full_name__icontains=q) | Q(user__phone__icontains=q)
                       | Q(listing__title__icontains=q) | Q(unit__code__iexact=q))
    return render(request, "agencies/orders.html", _ctx(
        request, "orders", page=_page(request, qs.order_by("-created_at")), status=status, plan=plan, q=q,
        statuses=Order.Status.choices, plans=Order.Plan.choices))


@agency_required()
def order_detail(request, reference):
    o = get_object_or_404(_orders(request), reference=reference)
    here = reverse("agency:order", args=[reference])
    if request.method == "POST":
        a, p = request.POST.get("action"), request.POST
        if a == "pay_link":
            if _do(request, lambda: services.start_checkout(o, count=int(p.get("count") or 1))):
                messages.success(request, "Payment link ready – open it now or send it to the customer.")
        elif a == "refresh":
            from apps.payments.services import refresh_from_provider

            for txn in PaymentTransaction.objects.filter(object_id=o.pk, purpose__in=["ORDER", "INSTALLMENT"],
                                                         status__in=["INITIATED", "PENDING"]):
                refresh_from_provider(txn)
            messages.info(request, "Payment status checked with the provider.")
        elif a == "office_payment":
            _do(request, lambda: ops.record_office_payment(o, request.user, amount=p.get("amount") or 0,
                                                           method=p.get("method", ""), reference=p.get("reference", ""),
                                                           note=p.get("note", ""), bank=p.get("bank", "")),
                "Payment recorded.")
        elif a == "handover":
            _do(request, lambda: order_svc.complete_handover(o, staff=request.user, note=p.get("note", "")),
                "Handover recorded – the customer has been notified.")
        elif a == "cancel":
            if not request.agency_staff.can_manage:
                messages.error(request, "Only the company owner or a manager can cancel orders.")
            else:
                _do(request, lambda: order_svc.cancel_order(o, p.get("reason") or f"Cancelled by {request.agency.name}",
                                                            staff=request.user), "Order cancelled – the unit is back on sale.")
        elif a == "note":
            _do(request, lambda: ops.add_note(o, request.user, p.get("note", "")), "Note added.")
        elif a in ("confirm_proof", "reject_proof", "dispute_proof"):
            from apps.market.models import BankTransferProof
            from apps.market.services import bank_transfer

            proof = get_object_or_404(BankTransferProof, pk=p.get("proof") or 0, order=o)
            if a == "confirm_proof":
                done = _do(request, lambda: bank_transfer.confirm_proof(
                    proof, request.user, amount=p.get("amount") or 0, payer_name=p.get("payer_name", ""),
                    credited_at=p.get("credited_at"), statement_ref=p.get("statement_ref", "")))
                if done:
                    proof.refresh_from_db()
                    msg = {"CONFIRMED": "Payment confirmed – the buyer has been notified.",
                           "DISPUTED": "Your statement doesn't match the receipt – Nebret will check it.",
                           }.get(proof.status, "First confirmation saved – a second owner or manager must confirm.")
                    (messages.success if proof.status != "DISPUTED" else messages.warning)(request, msg)
            elif a == "reject_proof":
                _do(request, lambda: bank_transfer.reject_proof(proof, request.user, p.get("reason", "")),
                    "Receipt rejected – the buyer has been told why.")
            else:
                _do(request, lambda: bank_transfer.dispute_proof(proof, request.user, p.get("reason", "")),
                    "Sent to Nebret to check.")
        elif a == "partner_refund":
            from apps.market.services import bank_transfer

            _do(request, lambda: bank_transfer.record_partner_refund(
                o, request.user, amount=p.get("amount") or 0, reference=p.get("reference", ""),
                refunded_on=p.get("refunded_on"), evidence=request.FILES.get("evidence")), "Refund recorded.")
        return redirect(here)
    payments = PaymentTransaction.objects.filter(purpose__in=["ORDER", "INSTALLMENT"], object_id=o.pk,
                                                 content_type__model="order").order_by("-created_at")
    live = next((t for t in payments if t.status in ("INITIATED", "PENDING") and t.checkout_url), None)
    return render(request, "agencies/order_detail.html", _ctx(
        request, "orders", o=o, payments=payments, live=live, installments=o.installments.all(),
        options=order_svc.installment_options(o) if o.status == Order.Status.INSTALLMENTS else [],
        events=o.events.select_related("by")[:40], entries=AgencyLedgerEntry.objects.filter(order=o),
        methods=ops.OFFICE_METHODS, proofs=o.proofs.select_related("partner_confirmed_by"),
        can_manage=request.agency_staff.can_manage,
        refund_owed=max(o.amount_paid - o.refunded_amount, 0) if o.status in (
            Order.Status.CANCELLED, Order.Status.DEFAULTED, Order.Status.EXPIRED) else 0))


@agency_required()
def handover_desk(request):
    """Type an order reference or scan the customer's handover pass."""
    found, payload = None, request.POST.get("payload", "").strip() if request.method == "POST" else ""
    if payload:
        found = handover_svc.verify_pass(payload)
        if not found or found.company_id != request.agency.id:
            messages.error(request, "That pass is not valid for your company.")
            found = None
    due = _orders(request).filter(status=Order.Status.PAID).order_by("handover_due")[:30]
    return render(request, "agencies/handover.html", _ctx(request, "handover", found=found, payload=payload, due=due))


@agency_required()
def sell(request):
    """Place an order for one of the company's customers."""
    c = _customer(request, request.GET.get("customer") or request.POST.get("customer") or 0) \
        if (request.GET.get("customer") or request.POST.get("customer")) else None
    live = _listings(request).filter(status=Listing.Status.ACTIVE)
    form = f.SaleForm(request.POST or None, listings=live,
                      initial={"listing": request.GET.get("listing"), "buyer_name": c.user.full_name if c else ""})
    if request.method == "POST" and c and form.is_valid():
        d = form.cleaned_data
        o = _do(request, lambda: order_svc.create_order(
            user=c.user, listing=d["listing"], plan=d["plan"], months=d.get("months") or 0,
            down_payment=d.get("down_payment"), delivery_method=d["delivery_method"],
            delivery_address=d.get("delivery_address", ""), buyer_name=d.get("buyer_name", ""),
            buyer_id_number=d.get("buyer_id_number", ""), by=request.user))
        if o:
            order_svc.log_event(o, "NOTE", f"Sold by {request.user.full_name} ({request.agency.name}).", by=request.user)
            messages.success(request, f"Order {o.reference} created – {o.first_payment:,.2f} ETB is due now. Record the "
                                      "payment or send the customer the payment link.")
            return redirect("agency:order", reference=o.reference)
    elif request.method == "POST" and not c:
        messages.error(request, "Choose the customer first.")
    return render(request, "agencies/sell.html", _ctx(
        request, "sell", c=c, form=form, customers=request.agency.customers.select_related("user")
        .order_by("user__full_name")))


# --------------------------------------------------------------------------- inquiries
@agency_required()
def inquiries(request):
    status = request.GET.get("status", "OPEN")
    qs = Inquiry.objects.filter(listing__company=request.agency).select_related("listing", "user")
    if request.method == "POST":
        inq = get_object_or_404(qs, pk=request.POST.get("inquiry"))
        reply = request.POST.get("reply", "").strip()
        if reply:
            from apps.notifications.models import Notification
            from apps.notifications.services import notify

            from apps.chat import services as chat

            inq.reply, inq.status = reply[:2000], Inquiry.Status.ANSWERED
            inq.replied_by, inq.replied_at = request.user, timezone.now()
            inq.save()
            try:
                # the buyer reads replies in the chat with the company (that is where the app shows them)
                chat.post_message(chat.for_listing(inq.user, inq.listing), request.user, reply)
            except BusinessRuleError as exc:
                notify(inq.user, Notification.Kind.ORDER, f"{request.agency.name} replied",
                       f"About {inq.listing.title}: {reply[:300]}", {"listing": inq.listing.slug})
                messages.warning(request, f"Reply saved, but not posted in the chat: {exc.detail}")
            else:
                messages.success(request, "Reply sent – the customer sees it in their chat with you.")
        elif request.POST.get("action") == "close":
            inq.status = Inquiry.Status.CLOSED
            inq.save(update_fields=["status", "updated_at"])
        return redirect(f"{reverse('agency:inquiries')}?status={status}")
    if status:
        qs = qs.filter(status=status)
    return render(request, "agencies/inquiries.html", _ctx(request, "inquiries", page=_page(request, qs), status=status,
                                                          statuses=Inquiry.Status.choices))


# --------------------------------------------------------------------------- chat with buyers
@agency_required()
def chats(request):
    from apps.chat import services as chat
    from apps.chat.models import Conversation, Message

    q, tab = request.GET.get("q", "").strip(), request.GET.get("tab", "all")
    qs = Conversation.objects.filter(agency=request.agency).select_related("buyer", "listing", "order")
    if q:
        qs = qs.filter(Q(buyer__full_name__icontains=q) | Q(listing__title__icontains=q) | Q(order__reference__iexact=q))
    rows = []
    for c in qs.order_by("-last_message_at", "-id")[:200]:
        last = c.messages.filter(deleted_at__isnull=True).order_by("-created_at", "-id").first()
        waiting = bool(last and last.sender_role == Message.Role.BUYER)
        if tab == "waiting" and not waiting:
            continue
        rows.append({"c": c, "last": last, "waiting": waiting, "unread": chat.unread_for(c, request.user)})
    return render(request, "agencies/chats.html", _ctx(request, "chats", rows=rows, q=q, tab=tab,
                                                       waiting=sum(1 for r in rows if r["waiting"]) if tab == "all" else len(rows)))


@agency_required()
def chat_detail(request, pk):
    from apps.chat import services as chat
    from apps.chat import web as chat_web
    from apps.chat.models import Conversation

    conv = get_object_or_404(Conversation.objects.select_related("buyer", "listing", "order"), pk=pk, agency=request.agency)
    here = reverse("agency:chat", args=[pk])
    if request.method == "GET" and chat_web.is_ajax(request):
        return chat_web.render_messages(request, conv)
    if request.method == "POST":
        return chat_web.send_from_web(request, conv, here)
    chat.mark_read(conv, request.user)
    return render(request, "agencies/chat.html", _ctx(
        request, "chats", c=conv, msgs=chat_web.thread_rows(conv, request.user, request), can_send=conv.status == "OPEN",
        poll_url=here, send_url=here))


# --------------------------------------------------------------------------- customers
@agency_required()
def customers(request):
    q = request.GET.get("q", "").strip()
    qs = services.search_customers(request.agency, q).annotate(
        n_family=Count("family", distinct=True),
        n_orders=Count("user__orders", filter=Q(user__orders__company=request.agency,
                                                user__orders__status__in=[*Order.LIVE_STATUSES, Order.Status.COMPLETED]),
                       distinct=True),
        n_equb=Count("user__equb_memberships", filter=Q(user__equb_memberships__status__in=EqubMembership.LIVE_STATUSES),
                     distinct=True)).order_by("-created_at")
    return render(request, "agencies/customers.html", _ctx(request, "customers", page=_page(request, qs), q=q,
                                                          total=request.agency.customers.count()))


@agency_required()
def customer_add(request):
    form = f.CustomerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        res = _do(request, lambda: services.add_customer(request.agency, full_name=d["full_name"], phone=d["phone"],
                                                         email=d["email"], password=d["password"], notes=d["notes"],
                                                         added_by=request.user))
        if res:
            cust, created = res
            messages.success(request, f"{cust.user.full_name} is now your customer"
                             + ("." if created else " (they already had a Nebret account)."))
            if request.POST.get("_another"):
                return redirect("agency:customer_add")
            return redirect("agency:customer", pk=cust.pk)
    return render(request, "agencies/form.html", _ctx(request, "customers", form=form, title="New customer",
                                                     cancel=reverse("agency:customers"), another=True))


@agency_required()
def customer_detail(request, pk):
    c = _customer(request, pk)
    here = reverse("agency:customer", args=[pk])
    if request.method == "POST":
        a, p = request.POST.get("action"), request.POST
        if a == "remove_family":
            FamilyMember.objects.filter(pk=p.get("member"), customer=c).delete()
            messages.success(request, "Family member removed.")
        elif a == "password":
            _do(request, lambda: services.set_customer_password(c, p.get("password", "")),
                f"{c.user.first_name} can now sign in to the app with their phone and this PIN.")
        elif a == "notes":
            c.notes = p.get("notes", "")[:2000]
            c.save(update_fields=["notes", "updated_at"])
            messages.success(request, "Notes saved.")
        return redirect(here)
    places = (EqubMembership.objects.filter(user=c.user).exclude(status=EqubMembership.Status.LEFT)
              .select_related("group").order_by("-created_at"))
    purchases = (Order.objects.filter(user=c.user, company=request.agency).select_related("listing", "unit")
                 .order_by("-created_at")[:20])
    return render(request, "agencies/customer_detail.html", _ctx(
        request, "customers", c=c, family=c.family.all(), places=places, purchases=purchases))


@agency_required()
def family_form(request, pk, member=None):
    c = _customer(request, pk)
    obj = get_object_or_404(FamilyMember, pk=member, customer=c) if member else None
    form = f.FamilyForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        fm = form.save(commit=False)
        fm.customer = c
        fm.save()
        messages.success(request, f"{fm.full_name} saved to {c.user.first_name}'s family.")
        if request.POST.get("_another"):
            return redirect("agency:family_add", pk=c.pk)
        return redirect("agency:customer", pk=c.pk)
    return render(request, "agencies/form.html", _ctx(
        request, "customers", form=form, obj=obj, another=not obj,
        title=f"{'Edit' if obj else 'Add'} family member · {c.user.full_name}",
        cancel=reverse("agency:customer", args=[c.pk])))


# --------------------------------------------------------------------------- Equb
@agency_required()
def equbs(request):
    a = request.agency
    groups = (EqubGroup.objects.filter(status__in=[EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE])
              .annotate(n=Count("memberships", filter=~Q(memberships__status=EqubMembership.Status.LEFT)))
              .order_by("status", "start_date"))
    mine = (EqubMembership.objects.filter(user__agency_link__agency=a).exclude(status=EqubMembership.Status.LEFT)
            .select_related("group", "user").order_by("-created_at"))
    return render(request, "agencies/equbs.html", _ctx(request, "equb", groups=groups, page=_page(request, mine)))


@agency_required()
def equb_enrol(request, slug):
    g = get_object_or_404(EqubGroup, slug=slug, status__in=[EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE])
    custs = request.agency.customers.select_related("user").prefetch_related("family").order_by("user__full_name")
    if request.method == "POST":
        c = _customer(request, request.POST.get("customer") or 0)
        who = request.POST.get("who", "self")
        fm = None
        if who != "self":
            fm = FamilyMember.objects.filter(pk=who, customer=c).first()
            if not fm:
                messages.error(request, "Choose who gets the place.")
                return redirect("agency:equb_enrol", slug=slug)
        m = _do(request, lambda: services.enrol(c, g, family_member=fm))
        if m:
            messages.success(request, f"{m.display_name} holds place #{m.position} in {g.name}"
                             + (f" – {c.user.first_name} pays for them." if fm else "."))
            return redirect("agency:customer", pk=c.pk)
        return redirect("agency:equb_enrol", slug=slug)
    return render(request, "agencies/equb_enrol.html", _ctx(
        request, "equb", g=g, customers=custs, pre=request.GET.get("customer", "")))


@agency_required()
def equb_place(request, pk):
    """One customer's Equb place: what is owed and a form to record money the company collected."""
    from apps.equb import services as equb_svc
    from apps.equb import standing as standing_mod
    from apps.equb.models import EqubPayment

    m = get_object_or_404(EqubMembership.objects.select_related("group", "user"), pk=pk,
                          user__agency_link__agency=request.agency)
    if request.method == "POST":
        p = request.POST
        if _do(request, lambda: services.collect_equb_payment(
                request.agency, m, request.user, method=p.get("method", ""),
                rounds=int(p["rounds"]) if p.get("rounds") else None, amount=p.get("amount") or None,
                reference=p.get("reference", ""), note=p.get("note", "")),
               "Payment recorded – the member has been notified."):
            return redirect("agency:equb_place", pk=pk)
    plan = equb_svc.payment_plan(m)
    return render(request, "agencies/equb_place.html", _ctx(
        request, "equb", m=m, st=standing_mod.standing_for(m), plan=plan,
        methods=[(k.value, k.label) for k in EqubPayment.OFFICE_METHODS],
        payments=m.payments.filter(status=EqubPayment.Status.PAID).select_related("recorded_by").order_by("-created_at")[:30],
        customer=AgencyCustomer.objects.filter(user=m.user, agency=request.agency).first()))


# --------------------------------------------------------------------------- earnings, team, profile
@agency_required()
def earnings(request):
    from . import commission as cm
    from apps.payments.banks import Channel

    a = request.agency
    if request.method == "POST" and request.POST.get("action") == "commission":
        try:
            cp = cm.submit(a, request.user, file=request.FILES.get("file"), bank=request.POST.get("bank", ""),
                           amount=request.POST.get("amount") or 0,
                           transfer_datetime=request.POST.get("transfer_datetime", ""),
                           reference=request.POST.get("reference", ""), payer_name=request.POST.get("payer_name", ""))
        except BusinessRuleError as exc:
            messages.error(request, str(exc.detail))
        else:
            messages.success(request, {"VERIFIED": "Verified by the bank – thank you, your commission is marked paid.",
                                       "REJECTED": f"The bank check failed: {cp.reason}"}.get(
                cp.status, "Receipt sent – it is being checked with the bank."))
        return redirect("agency:earnings")
    status = request.GET.get("payout", "")
    qs = a.ledger.select_related("customer", "order")
    if status:
        qs = qs.filter(payout_status=status)
    return render(request, "agencies/earnings.html", _ctx(
        request, "earnings", **_payment_accounts(a), page=_page(request, qs), total=services.summary(a), payout=status,
        payouts=AgencyLedgerEntry.Payout.choices, commission_payments=a.commission_payments.order_by("-created_at")[:10],
        commission_in_review=cm.in_review(a), banks=Channel.choices, now=timezone.localtime()))


@agency_required()
def team(request):
    a = request.agency
    form = f.StaffForm(request.POST or None)
    if request.method == "POST":
        if not request.agency_staff.can_manage:
            messages.error(request, "Only the company owner or a manager can manage the team.")
            return redirect("agency:team")
        if request.POST.get("action") == "toggle":
            st = get_object_or_404(AgencyStaff, pk=request.POST.get("staff"), agency=a)
            if st.role == AgencyStaff.Role.OWNER or st.user_id == request.user.id:
                messages.error(request, "You can't disable the owner or yourself.")
            else:
                st.is_active = not st.is_active
                st.save(update_fields=["is_active", "updated_at"])
                messages.success(request, f"{st.user.full_name} {'enabled' if st.is_active else 'disabled'}.")
            return redirect("agency:team")
        if form.is_valid():
            d = form.cleaned_data
            st = _do(request, lambda: services.add_staff(a, full_name=d["full_name"], phone=d["phone"],
                                                         password=d["password"], role=d["role"]))
            if st:
                messages.success(request, f"{st.user.full_name} added as {st.get_role_display().lower()}.")
                return redirect("agency:team")
    return render(request, "agencies/team.html", _ctx(
        request, "team", staff=a.staff.select_related("user"), form=form))


@agency_required()
def profile(request):
    form = f.CompanyProfileForm(request.POST or None, instance=request.agency)
    if request.method == "POST":
        if not request.agency_staff.can_manage:
            messages.error(request, "Only the company owner or a manager can edit the profile.")
        elif form.is_valid():
            form.save()
            messages.success(request, "Company profile saved.")
            return redirect("agency:profile")
    return render(request, "agencies/profile.html", _ctx(request, "profile", form=form,
                                                         **_payment_accounts(request.agency)))


@require_POST
def logout_view(request):
    from django.contrib.auth import logout

    logout(request)
    return redirect("console:login")


__all__ = ["Sum"]
