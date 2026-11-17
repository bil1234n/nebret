"""
Nebret Equb staff console — a server-rendered web dashboard for operations staff.

Every page is staff-only. Charts are rendered client-side with Chart.js from a
single JSON config emitted per page (see ``static/console/console.js``).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User, normalize_phone
from apps.accounts.services import approve_deletion, deletion_blockers, restore_account
from apps.equb.models import Contribution, EqubGroup, EqubMembership, EqubRound
from apps.luckydraw.models import CrowdfundingCampaign, Donation, Giveaway, GiveawayEntry, GiveawayWinner
from apps.market.models import Category, Inquiry, Installment, Listing, ListingUnit, Order
from apps.payments.models import PaymentTransaction

from . import forms as f

PAID = PaymentTransaction.Status.SUCCESS
ORDER_STATUS_ORDER = [("PENDING_PAYMENT", "Awaiting first payment"), ("RESERVED", "Reserved"),
                      ("INSTALLMENTS", "Paying installments"), ("PAID", "Paid – awaiting handover"),
                      ("COMPLETED", "Handed over"), ("CANCELLED", "Cancelled"), ("EXPIRED", "Expired"),
                      ("DEFAULTED", "Defaulted"), ("REFUNDED", "Refunded")]
ORDER_STATUS_COLORS = {"PENDING_PAYMENT": "#C9A227", "RESERVED": "#E6C766", "INSTALLMENTS": "#3E8E6C", "PAID": "#1B4D3E",
                       "COMPLETED": "#0F382B", "CANCELLED": "#C0392B", "EXPIRED": "#B7C4BE", "DEFAULTED": "#8E2A20",
                       "REFUNDED": "#E8A598"}
GROUP_COLORS = {"VEHICLES": "#0F382B", "HOMES": "#3E8E6C", "LAND": "#C9A227", "ELECTRONICS": "#8FBFA8"}
PURPOSE_LABELS = dict(PaymentTransaction.Purpose.choices)
SALES_PURPOSES = ("ORDER", "INSTALLMENT")
PER_PAGE = 25


# --------------------------------------------------------------------------- helpers
def staff_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('console:login')}?next={request.get_full_path()}")
        if not request.user.is_staff:
            from apps.agencies.services import staff_for

            if staff_for(request.user):
                return redirect("agency:dashboard")
            logout(request)
            messages.error(request, "The console is for Nebret Equb staff accounts only.")
            return redirect("console:login")
        return view(request, *args, **kwargs)
    return wrapper


def days_back(n: int):
    today = timezone.localdate()
    return [today - timedelta(days=i) for i in range(n - 1, -1, -1)]


def series(qs, date_field: str, value, days: list, extra_filter: dict | None = None):
    """Aggregate ``value`` per day over ``days`` → list aligned with days."""
    start = days[0]
    rows = (qs.filter(**{f"{date_field}__date__gte": start}, **(extra_filter or {}))
            .annotate(d=TruncDate(date_field)).values("d").annotate(v=value))
    by_day = {r["d"]: r["v"] or 0 for r in rows}
    return [float(by_day.get(d, 0)) for d in days]


def labels_for(days):
    return [d.strftime("%d %b") for d in days]


def page(request, qs):
    return Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))


def pct(a, b):
    return round(100 * float(a) / float(b), 1) if b else 0.0


def growth(cur, prev):
    if not prev:
        return None
    return round(100 * (float(cur) - float(prev)) / float(prev), 1)


# --------------------------------------------------------------------------- auth
def login_view(request):
    from apps.agencies.services import staff_for

    if request.user.is_authenticated and request.user.is_staff:
        return redirect("console:dashboard")
    if request.user.is_authenticated and staff_for(request.user):
        return redirect("agency:dashboard")
    error = None
    if request.method == "POST":
        phone = normalize_phone(request.POST.get("phone", ""))
        user = authenticate(request, username=phone, password=request.POST.get("password", ""))
        if user is None:
            error = "Incorrect phone number or password."
        elif not user.is_staff and staff_for(user):
            login(request, user)
            nxt = request.GET.get("next", "")
            return redirect(nxt if nxt.startswith("/company/") else "agency:dashboard")
        elif not user.is_staff:
            error = "This account does not have staff access or company access."
        else:
            login(request, user)
            nxt = request.GET.get("next", "")
            return redirect(nxt if nxt.startswith("/console/") else "console:dashboard")
    return render(request, "console/login.html", {"error": error, "phone": request.POST.get("phone", ""),
                                                  "debug": settings.DEBUG})


@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, "You have been signed out.")
    return redirect("console:login")


@staff_required
def profile(request):
    user = request.user
    pw_form = PasswordChangeForm(user)
    if request.method == "POST":
        if request.POST.get("form") == "profile":
            user.full_name = request.POST.get("full_name", user.full_name).strip() or user.full_name
            user.email = request.POST.get("email", "").strip()
            user.save(update_fields=["full_name", "email"])
            messages.success(request, "Profile updated.")
            return redirect("console:profile")
        pw_form = PasswordChangeForm(user, request.POST)
        if pw_form.is_valid():
            pw_form.save()
            update_session_auth_hash(request, pw_form.user)
            messages.success(request, "Password changed.")
            return redirect("console:profile")
    activity = {
        "draws_run": EqubRound.objects.filter(draw__executed_by=user).count(),
        "handovers": Order.objects.filter(handed_over_by=user).count(),
        "last_login": user.last_login,
        "joined": user.date_joined,
    }
    return render(request, "console/profile.html", {"pw_form": pw_form, "activity": activity, "nav": "profile"})


# --------------------------------------------------------------------------- dashboard
@staff_required
def dashboard(request):
    period = int(request.GET.get("days", 30)) if request.GET.get("days", "30").isdigit() else 30
    period = period if period in (7, 30, 90) else 30
    days = days_back(period)
    since = timezone.now() - timedelta(days=period)
    prev_since = since - timedelta(days=period)
    # Money kept: successful payments minus anything refunded since (fully refunded payments net to zero).
    # Crowdfunding gifts belong to the people they help, so they are not company revenue.
    paid = PaymentTransaction.objects.filter(status__in=[PAID, PaymentTransaction.Status.REFUNDED]).exclude(
        purpose__in=["DONATION", "MICRO_DONATION"])
    net = Sum(F("amount") - F("refunded_amount"))

    revenue = paid.filter(completed_at__gte=since).aggregate(s=net)["s"] or 0
    revenue_prev = paid.filter(completed_at__gte=prev_since, completed_at__lt=since).aggregate(s=net)["s"] or 0
    sold = Order.objects.filter(confirmed_at__gte=since).exclude(status__in=["CANCELLED", "REFUNDED"])
    sold_prev = Order.objects.filter(confirmed_at__gte=prev_since, confirmed_at__lt=since).exclude(
        status__in=["CANCELLED", "REFUNDED"])
    gmv = sold.aggregate(s=Sum("total_amount"))["s"] or 0
    customers = User.objects.filter(is_staff=False)
    total_customers = customers.count()
    new_customers = customers.filter(date_joined__gte=since).count()
    equb_saved = Contribution.objects.filter(status="PAID", paid_at__gte=since).aggregate(s=Sum("amount"))["s"] or 0
    gifts = Donation.objects.filter(status="PAID", paid_at__gte=since).aggregate(s=Sum("amount"), f=Sum("fee_amount"))
    crowd_raised, crowd_fees = gifts["s"] or 0, gifts["f"] or 0
    commission = paid.filter(completed_at__gte=since, agency__isnull=False).aggregate(s=Sum("platform_fee"))["s"] or 0
    rate = Decimal(str(settings.NEBRET["COMPANY_PROFIT_PERCENT"]))
    own_sales = paid.filter(completed_at__gte=since, purpose__in=SALES_PURPOSES, agency__isnull=True).aggregate(
        s=net)["s"] or 0
    profit = (Decimal(commission) + Decimal(own_sales) * rate / 100).quantize(Decimal("0.01"))
    receivable = Installment.objects.filter(order__status="INSTALLMENTS").exclude(status__in=["PAID", "WAIVED"])
    overdue = receivable.filter(status="LATE")

    kpis = [
        {"label": "Revenue (net of refunds)", "value": revenue, "money": True, "growth": growth(revenue, revenue_prev),
         "icon": "cash", "dark": True},
        {"label": "Orders confirmed", "value": sold.count(), "growth": growth(sold.count(), sold_prev.count()),
         "sub": f"{gmv:,.0f} ETB of property sold", "icon": "check"},
        {"label": "Customers", "value": f"{total_customers:,}", "sub": f"+{new_customers} joined in the last {period} days",
         "icon": "users"},
        {"label": "Platform income", "value": profit, "money": True,
         "sub": f"{commission:,.0f} ETB commission + {rate.normalize()}% of own sales", "icon": "cash", "gold": True},
        {"label": "Installments receivable", "value": sum((i.amount_due for i in receivable), Decimal("0")),
         "money": True, "sub": f"{overdue.count()} installment{'s' if overdue.count() != 1 else ''} overdue",
         "icon": "ticket"},
        {"label": "Equb savings", "value": equb_saved, "money": True,
         "sub": f"{EqubMembership.objects.exclude(status='LEFT').count()} active memberships", "icon": "people"},
        {"label": "Units for sale", "value": ListingUnit.objects.filter(status="AVAILABLE", listing__status="ACTIVE").count(),
         "sub": f"{Listing.objects.live().count()} live listings", "icon": "seat"},
        {"label": "Crowdfunding raised", "value": crowd_raised, "money": True,
         "sub": f"+{crowd_fees:,.0f} ETB platform fees · {GiveawayEntry.objects.filter(created_at__gte=since).aggregate(s=Sum('count'))['s'] or 0} free giveaway entries",
         "icon": "gift"},
    ]

    rev_by_purpose = [
        {"label": label, "data": series(paid, "completed_at", net, days, {"purpose__in": purposes})}
        for label, purposes in (("Marketplace", SALES_PURPOSES), ("Equb", ("EQUB_CONTRIBUTION",)))
    ]
    status_counts = dict(Order.objects.values_list("status").annotate(c=Count("id")))
    provider_rows = paid.filter(completed_at__gte=since).values("provider").annotate(s=net).order_by("-s")
    by_group = (Order.objects.filter(confirmed_at__isnull=False).exclude(status__in=["CANCELLED", "REFUNDED"])
                .values("listing__category__group").annotate(s=Sum("total_amount")).order_by("-s"))
    group_labels = dict(Category.Group.choices)
    top_listings = (Listing.objects.annotate(n=Count("orders", filter=Q(orders__confirmed_at__isnull=False)))
                    .filter(n__gt=0).order_by("-n")[:8])
    signups = series(User.objects.all(), "date_joined", Count("id"), days)
    plans = dict(Order.objects.filter(confirmed_at__isnull=False).values_list("plan").annotate(c=Count("id")))
    plan_labels = dict(Order.Plan.choices)

    charts = {
        "revenue": {"type": "bar", "stacked": True, "money": True, "labels": labels_for(days), "datasets": rev_by_purpose},
        "order_status": {"type": "doughnut", "labels": [label for key, label in ORDER_STATUS_ORDER if key in status_counts],
                         "datasets": [{"label": "Orders",
                                       "data": [status_counts[key] for key, _ in ORDER_STATUS_ORDER if key in status_counts],
                                       "colors": [ORDER_STATUS_COLORS[key] for key, _ in ORDER_STATUS_ORDER
                                                  if key in status_counts]}]},
        "providers": {"type": "doughnut", "money": True, "labels": [r["provider"].title() for r in provider_rows],
                      "datasets": [{"label": "Collected", "data": [float(r["s"]) for r in provider_rows]}]},
        "categories": {"type": "doughnut", "money": True,
                       "labels": [group_labels.get(r["listing__category__group"], "Other") for r in by_group],
                       "datasets": [{"label": "Sold", "data": [float(r["s"]) for r in by_group],
                                     "colors": [GROUP_COLORS.get(r["listing__category__group"], "#5B6F66")
                                                for r in by_group]}]},
        "top_listings": {"type": "bar", "horizontal": True, "labels": [lst.title[:30] for lst in top_listings],
                         "datasets": [{"label": "Orders", "data": [lst.n for lst in top_listings], "color": "#3E8E6C"}]},
        "plans": {"type": "doughnut", "labels": [plan_labels[p] for p in plans],
                  "datasets": [{"label": "Orders", "data": list(plans.values())}]},
        "signups": {"type": "line", "labels": labels_for(days), "datasets": [{"label": "New customers", "data": signups}]},
    }
    recent_orders = Order.objects.select_related("user", "listing", "unit")[:8]
    recent_payments = PaymentTransaction.objects.select_related("user")[:8]
    return render(request, "console/dashboard.html", {
        "nav": "dashboard", "kpis": kpis, "charts": charts, "period": period,
        "recent_orders": recent_orders, "recent_payments": recent_payments,
        "live_campaigns": CrowdfundingCampaign.objects.live()[:4], "live_giveaways": Giveaway.objects.live()[:4],
        "is_empty": not (Listing.objects.exists() or EqubGroup.objects.exists() or CrowdfundingCampaign.objects.exists()
                         or Giveaway.objects.exists()),
        "collecting_rounds": EqubRound.objects.filter(status="COLLECTING").select_related("group")[:5],
        "equb": _equb_snapshot(), "attention": _attention(),
    })


def _attention() -> dict:
    """Today's work queues across the marketplace and Equb – the strip at the top of the dashboard."""
    from apps.equb.models import EqubMembership as M

    now = timezone.now()
    week = timezone.localdate() + timedelta(days=7)
    return {
        "holds": Order.objects.filter(status="PENDING_PAYMENT", expires_at__gt=now).count(),
        "handovers": Order.objects.filter(status="PAID").count(),
        "handovers_late": Order.objects.filter(status="PAID", handover_due__lt=timezone.localdate()).count(),
        "overdue_installments": Installment.objects.filter(status="LATE", order__status="INSTALLMENTS").count(),
        "due_this_week": Installment.objects.filter(status="PENDING", order__status="INSTALLMENTS",
                                                    due_date__lte=week).count(),
        "refunds": Order.objects.filter(status__in=["CANCELLED", "DEFAULTED"], amount_paid__gt=F("refunded_amount")).count(),
        "inquiries": Inquiry.objects.filter(status="OPEN", listing__company__isnull=True).count(),
        "exits": M.objects.filter(exit_requested_at__isnull=False, status__in=M.LIVE_STATUSES).count(),
        "terminations": M.objects.filter(termination_requested_at__isnull=False, status__in=M.LIVE_STATUSES).count()
        + _pending_rejoins(),
    }


def _pending_rejoins() -> int:
    from apps.equb.models import JoinRequest

    return JoinRequest.objects.filter(status=JoinRequest.Status.PENDING).count()


def _with_standing(memberships):
    from apps.equb.standing import standings_for

    memberships = list(memberships)
    standings = standings_for(memberships)
    for m in memberships:
        m.standing = standings[m.id]
    return memberships


def _equb_snapshot() -> dict:
    from apps.equb import reports as equb_reports

    members, standings = equb_reports.live_standings()
    return equb_reports.receivables_summary(members, standings)


# --------------------------------------------------------------------------- payments
@staff_required
def payments(request):
    qs = PaymentTransaction.objects.select_related("user", "agency")
    status, purpose, provider, q = (request.GET.get(k, "") for k in ("status", "purpose", "provider", "q"))
    if status:
        qs = qs.filter(status=status)
    if purpose:
        qs = qs.filter(purpose=purpose)
    if provider:
        qs = qs.filter(provider=provider)
    if q:
        qs = qs.filter(Q(user__phone__icontains=q) | Q(provider_reference__icontains=q) | Q(user__full_name__icontains=q))
    days = days_back(30)
    by_status = dict(PaymentTransaction.objects.values_list("status").annotate(c=Count("id")))
    charts = {
        "volume": {"type": "line", "money": True, "labels": labels_for(days), "datasets": [
            {"label": "Successful", "data": series(PaymentTransaction.objects.filter(status=PAID), "completed_at", Sum("amount"), days)},
            {"label": "Failed", "data": series(PaymentTransaction.objects.filter(status="FAILED"), "created_at", Sum("amount"), days), "color": "#C0392B", "fill": False}]},
        "status": {"type": "doughnut", "labels": [s.title() for s in by_status], "datasets": [{"label": "Transactions", "data": list(by_status.values())}]},
    }
    agg = qs.aggregate(n=Count("id"), s=Sum("amount", filter=Q(status=PAID)))
    return render(request, "console/payments.html", {
        "nav": "payments", "page": page(request, qs), "charts": charts, "agg": agg, "status": status, "purpose": purpose,
        "provider": provider, "q": q, "statuses": PaymentTransaction.Status.choices, "purposes": PaymentTransaction.Purpose.choices,
        "providers": PaymentTransaction.Provider.choices,
    })


# --------------------------------------------------------------------------- customers
@staff_required
def users(request):
    qs = User.objects.annotate(
        n_orders=Count("orders", filter=Q(orders__confirmed_at__isnull=False), distinct=True),
        n_equbs=Count("equb_memberships", distinct=True),
        spent=Sum("payments__amount", filter=Q(payments__status=PAID)),
    ).order_by("-date_joined")
    q, role = request.GET.get("q", "").strip(), request.GET.get("role", "")
    if q:
        qs = qs.filter(Q(phone__icontains=q) | Q(full_name__icontains=q) | Q(email__icontains=q) | Q(referral_code__iexact=q)
                       | Q(national_id__iexact=q))
    if role == "staff":
        qs = qs.filter(is_staff=True)
    elif role == "customers":
        qs = qs.filter(is_staff=False)
    return render(request, "console/users.html", {"nav": "users", "page": page(request, qs), "q": q, "role": role,
                                                   "total": User.objects.count()})


@staff_required
def user_add(request):
    """Register a customer at the desk (they can sign in to the app once they have a PIN)."""
    form = f.CustomerForm(request.POST or None, instance=User(is_active=True))
    if request.method == "POST" and form.is_valid():
        u = form.save(commit=False)
        if not form.cleaned_data.get("new_password"):
            u.set_unusable_password()
        u.save()
        if form.cleaned_data.get("new_password"):
            u.set_password(form.cleaned_data["new_password"])
            u.save(update_fields=["password"])
        messages.success(request, f"{u.full_name} was added." + (
            f" They sign in to the app with {u.phone} and the PIN you set." if form.cleaned_data.get("new_password")
            else " Set a PIN later so they can sign in to the app."))
        return redirect("console:user", pk=u.pk)
    from .manage_views import _render
    return _render(request, nav="users", title="Add customer", crumbs="Customers", form=form,
                   cancel=reverse("console:users"), obj=None,
                   intro="Register a customer who came to the office or called. Phone number is their app sign-in.")


@staff_required
def user_edit(request, pk):
    u = get_object_or_404(User, pk=pk, is_staff=False)
    form = f.CustomerForm(request.POST or None, instance=u)
    if request.method == "POST" and form.is_valid():
        form.save()
        msg = f"{u.full_name}'s profile was saved."
        if form.cleaned_data.get("new_password"):
            msg += f" They can now sign in with {u.phone} and the new password."
        messages.success(request, msg)
        nxt = request.POST.get("next") or request.GET.get("next")
        return redirect(nxt if nxt and nxt.startswith("/console/") else reverse("console:user", args=[u.pk]))
    from .manage_views import _render
    return _render(request, nav="users", title=f"Edit {u.full_name}", crumbs="Customers", form=form,
                   cancel=request.GET.get("next") or reverse("console:user", args=[u.pk]), obj=u,
                   intro="" if u.has_usable_password() else
                   "This customer was registered at the desk and has no password yet – set one below so they can sign in.")


@staff_required
def user_detail(request, pk):
    from apps.accounts.models import KycProfile
    from apps.accounts.services import review_kyc

    u = get_object_or_404(User, pk=pk)
    kyc = KycProfile.objects.filter(user=u).defer("photo", "id_photo").select_related("reviewed_by").first()
    if request.method == "POST" and kyc and request.POST.get("action") in ("kyc_approve", "kyc_reject"):
        approve = request.POST["action"] == "kyc_approve"
        review_kyc(kyc, request.user, approve=approve, note=request.POST.get("note", ""))
        messages.success(request, "Profile approved." if approve else "Profile rejected – the member was asked to send it again.")
        return redirect("console:user", pk=pk)
    spent = dict(PaymentTransaction.objects.filter(user=u, status=PAID).values_list("purpose").annotate(s=Sum("amount")))
    charts = {"spend": {"type": "doughnut", "money": True, "labels": [PURPOSE_LABELS[k].split(" (")[0] for k in spent],
                        "datasets": [{"label": "Paid", "data": [float(v) for v in spent.values()]}]}}
    return render(request, "console/user_detail.html", {
        "nav": "users", "u": u, "charts": charts, "total_spent": sum(spent.values(), Decimal("0")),
        "orders": u.orders.select_related("listing", "unit", "company")[:20],
        "memberships": _with_standing(u.equb_memberships.select_related("group", "won_round")),
        "entries": u.giveaway_progress.filter(referrals__gt=0).select_related("giveaway"),
        "donations": Donation.objects.filter(user=u, status="PAID").select_related("campaign")[:10],
        "wins": GiveawayWinner.objects.filter(user=u).select_related("giveaway"),
        "payments": PaymentTransaction.objects.filter(user=u)[:15],
        "referrals": u.referrals.all()[:10],
        "blockers": deletion_blockers(u) if u.deletion_pending else None,
        "kyc": kyc, "kyc_has_id_photo": bool(kyc) and KycProfile.objects.filter(pk=kyc.pk, id_photo__isnull=False).exists(),
    })


@staff_required
def kyc_photo(request, pk, which):
    """A member's profile photo or ID picture (stored in the database), for staff only."""
    from django.http import Http404, HttpResponse

    from apps.accounts.models import KycProfile

    profile = get_object_or_404(KycProfile, user_id=pk)
    data = profile.photo if which == "photo" else profile.id_photo
    if not data:
        raise Http404
    resp = HttpResponse(bytes(data), content_type="image/jpeg")
    resp["Cache-Control"] = "private, max-age=300"
    return resp


# --------------------------------------------------------------------------- account deletion requests
@staff_required
def deletion_requests(request):
    """Customers who asked to delete their account (suspended until staff approve or restore)."""
    tab = request.GET.get("tab", "pending")
    if tab == "done":
        qs = (User.objects.filter(deletion_reviewed_at__isnull=False)
              .select_related("deletion_reviewed_by").order_by("-deletion_reviewed_at"))
    else:
        tab = "pending"
        qs = User.objects.filter(deletion_status=User.DeletionStatus.PENDING).order_by("deletion_requested_at")
    pg = page(request, qs)
    if tab == "pending":
        for u in pg:
            u.blockers = deletion_blockers(u)
    return render(request, "console/deletion_requests.html", {
        "nav": "deletions", "page": pg, "tab": tab,
        "pending_count": User.objects.filter(deletion_status=User.DeletionStatus.PENDING).count(),
    })


@staff_required
@require_POST
def deletion_decide(request, pk):
    u = get_object_or_404(User, pk=pk, is_staff=False)
    action, note = request.POST.get("action"), request.POST.get("note", "").strip()
    if not u.deletion_pending:
        messages.error(request, "This account has no pending deletion request.")
    elif action == "approve":
        name = u.full_name
        approve_deletion(u, by=request.user, note=note)
        messages.success(request, f"{name}'s account was deleted. Their personal data is gone; payment and order "
                                  f"records are kept without their name.")
    elif action == "restore":
        restore_account(u, by=request.user, note=note)
        messages.success(request, f"{u.full_name}'s account was restored – they can sign in again.")
    else:
        messages.error(request, "Choose approve or restore.")
    nxt = request.POST.get("next", "")
    return redirect(nxt if nxt.startswith("/console/") else "console:deletion_requests")
