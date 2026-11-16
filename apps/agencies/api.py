"""
Company workspace API (``/api/company/``) – what the mobile app shows when partner-company staff sign in.

It is the same workspace as the web portal at ``/company/``: the company's listings and stock, orders, office
payments and handovers, customer inquiries, customers, their Equb places and the company's earnings. Every query is
scoped to the signed-in staff member's company.
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from apps.core.exceptions import BusinessRuleError
from apps.equb import services as equb_svc
from apps.equb import standing as standing_mod
from apps.equb.models import EqubGroup, EqubMembership, EqubPayment
from apps.market.models import BankTransferProof, Category, Inquiry, Listing, ListingUnit, Order
from apps.market.serializers import (InquirySerializer, ListingDetailSerializer, ListingListSerializer,
                                     OrderDetailSerializer, OrderListSerializer)
from apps.market.services import handover as handover_svc
from apps.market.services import operations as ops
from apps.market.services import orders as order_svc

from . import services
from .bank_accounts import has_usable_account
from .models import AgencyCustomer, AgencyLedgerEntry, FamilyMember

ZERO = Decimal("0")


class IsCompanyStaff(permissions.BasePermission):
    message = "This area is for partner-company staff."

    def has_permission(self, request, view):
        st = services.staff_for(request.user)
        if not st:
            return False
        request.agency, request.agency_staff = st.agency, st
        return True


def company_api(methods=("GET",)):
    def deco(fn):
        return api_view(list(methods))(permission_classes([permissions.IsAuthenticated, IsCompanyStaff])(fn))
    return deco


def _need_manager(request):
    if not request.agency_staff.can_manage:
        raise BusinessRuleError("Only the company owner or a manager can do this.")


def _paged(request, qs, row, size: int = 30):
    pager = PageNumberPagination()
    pager.page_size = size
    page = pager.paginate_queryset(qs, request)
    return pager.get_paginated_response([row(o) for o in page])


def _ctx(request):
    return {"request": request, "saved_ids": set()}


def _listings(request):
    return (Listing.objects.filter(company=request.agency).select_related("category", "company")
            .prefetch_related("images")
            .annotate(_available=Count("units", filter=Q(units__status=ListingUnit.Status.AVAILABLE), distinct=True)))


def _orders(request):
    return (services.company_orders(request.agency)
            .select_related("user", "listing__category", "unit", "company").prefetch_related("installments"))


def mask_phone(phone: str) -> str:
    """Company staff never see a buyer's phone number – they talk in the in-app chat."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return ("•" * max(len(digits) - 2, 0) + digits[-2:]) if digits else ""


def _order_row(request, o: Order, detail: bool = False) -> dict:
    data = (OrderDetailSerializer if detail else OrderListSerializer)(o, context=_ctx(request)).data
    data["customer"] = {"name": o.user.full_name, "phone": mask_phone(o.user.phone)}
    if detail:
        data["contact_phone"] = mask_phone(data.get("contact_phone", ""))
        data["contact_email"] = ""
        data["events"] = [{"kind": e.kind, "kind_label": e.get_kind_display(), "message": e.message,
                           "created_at": e.created_at, "by": e.by.full_name if e.by_id else ""}
                          for e in o.events.select_related("by")[:40]]
        data["earnings"] = [_ledger_row(e) for e in AgencyLedgerEntry.objects.filter(order=o)]
    return data


def _ledger_row(e: AgencyLedgerEntry) -> dict:
    return {"id": e.id, "created_at": e.created_at, "kind": e.kind, "description": e.description,
            "customer": e.customer.full_name if e.customer_id else "", "order": e.order.reference if e.order_id else None,
            "gross_amount": str(e.gross_amount), "commission": str(e.commission), "agency_amount": str(e.agency_amount),
            "payout_status": e.payout_status, "payout_label": e.get_payout_status_display(),
            "direction": e.direction, "direction_label": e.get_direction_display()}


def _summary(d: dict) -> dict:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


def _customer_row(c: AgencyCustomer) -> dict:
    return {"id": c.id, "name": c.user.full_name, "phone": c.user.phone, "since": c.created_at,
            "can_sign_in": c.user.has_usable_password(), "notes": c.notes,
            "family_count": getattr(c, "n_family", None), "orders_count": getattr(c, "n_orders", None),
            "equb_count": getattr(c, "n_equb", None)}


def _place_row(m: EqubMembership, st=None) -> dict:
    st = st or standing_mod.standing_for(m)
    g = m.group
    return {"id": m.id, "member": m.display_name, "paid_by": m.user.full_name if m.dependent_name else "",
            "customer_phone": m.user.phone, "position": m.position, "status": m.status,
            "status_label": m.get_status_display(), "has_won": m.has_won, "total_paid": str(m.total_paid),
            "group": {"name": g.name, "slug": g.slug, "status": g.status, "contribution_amount": str(g.contribution_amount),
                      "every": g.every, "current_round": g.current_round, "total_rounds": g.total_rounds},
            "standing": {"status": st.status, "label": st.status_label, "tone": st.tone, "owes": str(st.owes),
                         "paid_rounds": st.paid_rounds, "next_due_date": st.next_due_date,
                         "next_due_amount": str(st.next_due_amount)}}


def _company(request) -> dict:
    a, st = request.agency, request.agency_staff
    return {"id": a.id, "name": a.name, "slug": a.slug, "kind": a.kind, "kind_label": a.get_kind_display(),
            "status": a.status, "status_label": a.get_status_display(), "is_active": a.is_active,
            "is_verified": a.is_verified, "logo_url": a.logo_url, "phone": a.phone, "email": a.email, "city": a.city,
            "address": a.address, "description": a.description, "website": a.website,
            "commission_percent": str(a.commission_percent), "role": st.role, "role_label": st.get_role_display(),
            "can_manage": st.can_manage}


# --------------------------------------------------------------------------- dashboard
@company_api()
def dashboard(request):
    a = request.agency
    since = timezone.make_aware(datetime.combine(timezone.localdate().replace(day=1), time.min))
    orders = _orders(request)
    places = EqubMembership.objects.filter(user__agency_link__agency=a).exclude(status=EqubMembership.Status.LEFT)
    return Response({
        "company": _company(request),
        "month": _summary(services.summary(a, since=since)), "total": _summary(services.summary(a)),
        "counts": {
            "listings": Listing.objects.filter(company=a, status=Listing.Status.ACTIVE).count(),
            "units": ListingUnit.objects.filter(listing__company=a, status=ListingUnit.Status.AVAILABLE).count(),
            "active_orders": orders.filter(status__in=Order.LIVE_STATUSES).count(),
            "to_pay": orders.filter(status=Order.Status.PENDING_PAYMENT).count(),
            "installments": orders.filter(status=Order.Status.INSTALLMENTS).count(),
            "handovers": orders.filter(status=Order.Status.PAID).count(),
            "inquiries": Inquiry.objects.filter(listing__company=a, status=Inquiry.Status.OPEN).count(),
            "customers": a.customers.count(), "equb_places": places.count(),
            "payments_to_confirm": BankTransferProof.objects.filter(
                order__company=a, status=BankTransferProof.Status.VERIFIED_AWAITING_PARTNER).count()},
        "bank_account_ready": has_usable_account(a),
        "pending": [_order_row(request, o) for o in orders.filter(status=Order.Status.PENDING_PAYMENT)
                    .order_by("expires_at")[:5]],
        "handovers": [_order_row(request, o) for o in orders.filter(status=Order.Status.PAID).order_by("handover_due")[:5]],
        "recent": [_ledger_row(e) for e in a.ledger.select_related("customer", "order").order_by("-created_at")[:6]],
    })


# --------------------------------------------------------------------------- listings & stock
class CompanyListingSerializer(serializers.ModelSerializer):
    category = serializers.SlugRelatedField(slug_field="slug", queryset=Category.objects.filter(is_active=True))
    units = serializers.IntegerField(write_only=True, required=False, min_value=0, max_value=500)

    class Meta:
        model = Listing
        fields = ["category", "title", "summary", "description", "brand", "model_name", "year", "condition", "city",
                  "area", "address", "price", "compare_at_price", "image_url", "specs", "features",
                  "allow_full_payment", "allow_reservation", "reservation_fee", "reservation_days", "allow_installments",
                  "min_down_payment_percent", "max_installment_months", "delivery_available",
                  "delivery_fee", "handover_days", "warranty_months", "units"]

    def validate(self, attrs):
        from django.core.exceptions import ValidationError as DjangoValidationError

        data = {**{f: getattr(self.instance, f) for f in self.Meta.fields if self.instance and f != "units"}, **attrs}
        data.pop("units", None)
        probe = Listing(**data)
        try:
            probe.clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(getattr(exc, "message_dict", None) or exc.messages)
        return attrs


def _listing_detail(request, listing: Listing) -> dict:
    data = ListingDetailSerializer(listing, context=_ctx(request)).data
    units = list(listing.units.all())
    data["raw_specs"] = listing.specs or {}
    data["stock"] = {s: sum(1 for u in units if u.status == s) for s in ListingUnit.Status.values}
    data["all_units"] = [{"id": u.id, "code": u.code, "label": u.label, "status": u.status,
                          "status_label": u.get_status_display(), "price": str(u.price)} for u in units[:300]]
    data["orders_count"] = listing.orders.filter(status__in=[*Order.LIVE_STATUSES, Order.Status.COMPLETED]).count()
    return data


@company_api(["GET", "POST"])
def listings(request):
    if request.method == "POST":
        _need_manager(request)
        s = CompanyListingSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        units = s.validated_data.pop("units", 1)
        listing = Listing(**s.validated_data, company=request.agency, created_by=request.user)
        from .bank_accounts import has_usable_account

        if request.agency.status != "ACTIVE" or not has_usable_account(request.agency):
            listing.status = Listing.Status.DRAFT
        listing.save()
        if units:
            listing.add_units(units)
        return Response(_listing_detail(request, _listings(request).get(pk=listing.pk)), status=201)
    qs = _listings(request)
    p = request.query_params
    if p.get("status"):
        qs = qs.filter(status=p["status"].upper())
    if p.get("q"):
        q = p["q"].strip()
        qs = qs.filter(Q(title__icontains=q) | Q(brand__icontains=q) | Q(model_name__icontains=q))
    return _paged(request, qs.order_by("-created_at"), lambda l: ListingListSerializer(l, context=_ctx(request)).data)


@company_api(["GET", "PATCH", "POST"])
def listing(request, pk: int):
    """GET detail · PATCH fields · POST ``{action: publish|unpublish|add_units|block|unblock|rename, ...}``."""
    obj = get_object_or_404(_listings(request), pk=pk)
    if request.method == "PATCH":
        _need_manager(request)
        s = CompanyListingSerializer(obj, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        s.validated_data.pop("units", None)
        for k, v in s.validated_data.items():
            setattr(obj, k, v)
        obj.save()
    elif request.method == "POST":
        _need_manager(request)
        d, action = request.data, request.data.get("action")
        if action in ("publish", "unpublish"):
            if action == "publish" and request.agency.status != "ACTIVE":
                raise BusinessRuleError("Your company must be approved before listings go live.")
            if action == "publish":
                from .bank_accounts import has_usable_account

                if not has_usable_account(request.agency):
                    raise BusinessRuleError("Add a bank account and wait for Nebret's approval before publishing – "
                                            "buyers pay straight into it.")
            obj.status = Listing.Status.ACTIVE if action == "publish" else Listing.Status.DRAFT
            obj.save(update_fields=["status", "updated_at"])
            obj.refresh_stock_status()
        elif action == "add_units":
            try:
                count = int(d.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            if not 1 <= count <= 500:
                raise BusinessRuleError("Add between 1 and 500 units.")
            obj.add_units(count, prefix=str(d.get("prefix") or "")[:12])
        elif action in ("block", "unblock", "rename"):
            unit = get_object_or_404(ListingUnit, pk=d.get("unit"), listing=obj)
            if action == "rename":
                unit.label = str(d.get("label") or "")[:120]
                unit.save(update_fields=["label"])
            elif unit.status in (ListingUnit.Status.AVAILABLE, ListingUnit.Status.BLOCKED):
                unit.status = ListingUnit.Status.BLOCKED if action == "block" else ListingUnit.Status.AVAILABLE
                unit.save(update_fields=["status"])
                obj.refresh_stock_status()
            else:
                raise BusinessRuleError("Units that are held, reserved or sold can't be changed.")
        else:
            raise BusinessRuleError("Unknown action.")
    return Response(_listing_detail(request, _listings(request).get(pk=pk)))


# --------------------------------------------------------------------------- orders, payments, handover
ORDER_TABS = {
    "to_pay": Q(status=Order.Status.PENDING_PAYMENT),
    "active": Q(status__in=[Order.Status.RESERVED, Order.Status.INSTALLMENTS]),
    "handover": Q(status=Order.Status.PAID),
    "completed": Q(status=Order.Status.COMPLETED),
    "closed": Q(status__in=[Order.Status.CANCELLED, Order.Status.EXPIRED, Order.Status.REFUNDED, Order.Status.DEFAULTED]),
}


@company_api()
def orders(request):
    qs = _orders(request)
    tab, q = request.query_params.get("tab"), (request.query_params.get("q") or "").strip()
    if tab in ORDER_TABS:
        qs = qs.filter(ORDER_TABS[tab])
    if q:
        qs = qs.filter(Q(reference__iexact=q) | Q(user__full_name__icontains=q) | Q(user__phone__icontains=q)
                       | Q(listing__title__icontains=q) | Q(unit__code__iexact=q))
    return _paged(request, qs.order_by("-created_at"), lambda o: _order_row(request, o))


@company_api(["GET", "POST"])
def order(request, reference: str):
    """GET detail · POST ``{action: payment|handover|cancel|note|pay_link, ...}``."""
    o = get_object_or_404(_orders(request), reference=reference.upper())
    extra = {}
    if request.method == "POST":
        d, action = request.data, request.data.get("action")
        if action == "payment":
            ops.record_office_payment(o, request.user, amount=d.get("amount") or 0, method=d.get("method", ""),
                                      reference=d.get("reference", ""), note=d.get("note", ""),
                                      bank=d.get("bank", ""))
        elif action == "handover":
            order_svc.complete_handover(o, staff=request.user, note=d.get("note", ""))
        elif action == "cancel":
            _need_manager(request)
            order_svc.cancel_order(o, d.get("reason") or f"Cancelled by {request.agency.name}", staff=request.user)
        elif action == "note":
            ops.add_note(o, request.user, d.get("note", ""))
        elif action == "pay_link":
            txn = services.start_checkout(o, count=int(d.get("count") or 1))
            extra["checkout_url"] = getattr(txn, "checkout_url", "")
        elif action == "proof":  # a receipt the buyer brought in, uploaded for them
            from apps.market.services import bank_transfer

            bank_transfer.submit_proof(
                o, request.user, file=request.FILES.get("file"), bank=d.get("bank", ""),
                reference=d.get("reference", ""), amount=d.get("amount") or 0,
                transfer_datetime=d.get("transfer_datetime", ""), payer_name=d.get("payer_name", ""),
                payer_account=d.get("payer_account", ""),
                declare_co_payer=str(d.get("declare_co_payer", "")).lower() in ("1", "true", "yes"))
        elif action == "refund":
            from apps.market.services import bank_transfer

            bank_transfer.record_partner_refund(o, request.user, amount=d.get("amount") or 0,
                                                reference=d.get("reference", ""), refunded_on=d.get("refunded_on"),
                                                evidence=request.FILES.get("evidence"), note=d.get("note", ""))
        else:
            raise BusinessRuleError("Unknown action.")
        o = _orders(request).get(pk=o.pk)
    from apps.market.services import bank_transfer

    extra["proofs"] = [bank_transfer.proof_row(p, request, for_staff=True) for p in o.proofs.select_related("order")]
    if o.status in (Order.Status.PENDING_PAYMENT, Order.Status.RESERVED, Order.Status.INSTALLMENTS) \
            and o.is_bank_transfer:
        extra["bank_instructions"] = bank_transfer.bank_instructions(o)
    return Response({**_order_row(request, o, detail=True), **extra})


# --------------------------------------------------------------------------- payments to confirm
PROOF_TABS = {"to_confirm": ["VERIFIED_AWAITING_PARTNER"], "checking": ["SUBMITTED", "VERIFYING"],
              "disputed": ["DISPUTED"], "done": ["CONFIRMED", "REJECTED", "EXPIRED"]}


@company_api()
def payments(request):
    """Receipts on the company's orders. ``?tab=to_confirm|checking|disputed|done``."""
    from apps.market.models import BankTransferProof
    from apps.market.services import bank_transfer

    qs = BankTransferProof.objects.filter(order__company=request.agency).select_related("order__user",
                                                                                         "partner_confirmed_by")
    tab = request.query_params.get("tab") or "to_confirm"
    if tab in PROOF_TABS:
        qs = qs.filter(status__in=PROOF_TABS[tab])
    return _paged(request, qs.order_by("created_at" if tab == "to_confirm" else "-created_at"),
                  lambda p: bank_transfer.proof_row(p, request, for_staff=True))


@company_api(["GET", "POST"])
def payment(request, pk: int):
    """POST ``{action: confirm, amount, payer_name, credited_at, statement_ref}`` · ``{action: reject, reason}`` ·
    ``{action: dispute, reason}``. Only owners and managers; above the threshold two different people confirm."""
    from apps.market.models import BankTransferProof
    from apps.market.services import bank_transfer

    proof = get_object_or_404(BankTransferProof.objects.select_related("order__user"), pk=pk,
                              order__company=request.agency)
    if request.method == "POST":
        d, action = request.data, request.data.get("action")
        if action == "confirm":
            proof = bank_transfer.confirm_proof(proof, request.user, amount=d.get("amount") or 0,
                                                payer_name=d.get("payer_name", ""), credited_at=d.get("credited_at"),
                                                statement_ref=d.get("statement_ref", ""))
        elif action == "reject":
            proof = bank_transfer.reject_proof(proof, request.user, d.get("reason", ""))
        elif action == "dispute":
            proof = bank_transfer.dispute_proof(proof, request.user, d.get("reason", ""))
        else:
            raise BusinessRuleError("Unknown action.")
        proof.refresh_from_db()
    return Response(bank_transfer.proof_row(proof, request, for_staff=True))


# --------------------------------------------------------------------------- bank accounts
@company_api(["GET", "POST"])
def bank_accounts(request):
    """GET – the company's accounts and their history. POST (owner) – submit a new or changed account:
    ``{bank, account_name, account_number, currency, swift_code, branch, replaces}``."""
    from . import bank_accounts as ba
    from .models import PartnerBankAccount

    if request.method == "POST":
        d = request.data
        replaces = None
        if d.get("replaces"):
            replaces = get_object_or_404(PartnerBankAccount, pk=d["replaces"], agency=request.agency)
        acc = ba.submit_account(request.agency, request.user, bank=d.get("bank", ""),
                                account_name=d.get("account_name", ""), account_number=d.get("account_number", ""),
                                currency=d.get("currency", "ETB"), swift_code=d.get("swift_code", ""),
                                branch=d.get("branch", ""), replaces=replaces)
        return Response(ba.account_row(acc), status=201)
    from apps.payments.banks import Bank, Currency

    return Response({"accounts": [ba.account_row(a) for a in request.agency.bank_accounts.all()],
                     "has_usable_account": ba.has_usable_account(request.agency),
                     "banks": [{"code": c, "name": n} for c, n in Bank.choices],
                     "currencies": list(Currency.values), "can_edit": request.agency_staff.role == "OWNER",
                     "cooling_off_hours": settings.NEBRET["BANK_ACCOUNT_COOLING_OFF_HOURS"]})


@company_api(["POST"])
def bank_account(request, pk: int):
    """``{action: retire, note}`` (owner)."""
    from . import bank_accounts as ba
    from .models import PartnerBankAccount

    acc = get_object_or_404(PartnerBankAccount, pk=pk, agency=request.agency)
    if request.data.get("action") != "retire":
        raise BusinessRuleError("Unknown action.")
    return Response(ba.account_row(ba.retire_account(acc, request.user, request.data.get("note", ""))))


# --------------------------------------------------------------------------- product Equbs
@company_api(["GET", "POST"])
def listing_equbs(request, pk: int):
    """GET – product Equbs for the listing. POST (owner/manager) – create one:
    ``{contribution_amount, duration_value, duration_unit, members, start_date, rounds_drawn, name, terms}``.
    ``?preview=1`` returns the maths without creating anything."""
    from django.utils.dateparse import parse_date

    from apps.equb import product
    from apps.equb.models import EqubGroup

    obj = get_object_or_404(_listings(request), pk=pk)
    if request.method == "POST":
        _need_manager(request)
        d = request.data
        if request.query_params.get("preview"):
            from apps.equb.models import EqubSettings

            fee = EqubSettings.load().service_fee_percent
            return Response(product.maths(price=obj.price, contribution=d.get("contribution_amount") or 0,
                                          duration_value=int(d.get("duration_value") or 0), fee_percent=fee))
        start = parse_date(str(d.get("start_date") or ""))
        if not start:
            raise BusinessRuleError("Choose the start date.")
        group, m = product.create_product_equb(
            obj, request.user, contribution_amount=d.get("contribution_amount") or 0,
            duration_value=d.get("duration_value") or 0, duration_unit=d.get("duration_unit") or "DAYS",
            members=d.get("members") or 0, start_date=start, rounds_drawn=d.get("rounds_drawn") or None,
            name=d.get("name", ""), terms=d.get("terms", ""))
        return Response({**product.group_row(group), "maths": m}, status=201)
    return Response([product.group_row(g) for g in EqubGroup.objects.filter(listing=obj).order_by("-created_at")])


@company_api(["POST"])
def handover_check(request):
    """``{payload}`` – an order reference typed by staff, or the customer's handover pass."""
    found = handover_svc.verify_pass(str(request.data.get("payload") or "").strip())
    if not found or found.company_id != request.agency.id:
        raise BusinessRuleError("That pass is not valid for your company.")
    return Response(_order_row(request, _orders(request).get(pk=found.pk), detail=True))


@company_api(["POST"])
def sell(request):
    """Place an order for one of the company's customers."""
    d = request.data
    c = get_object_or_404(AgencyCustomer.objects.select_related("user"), pk=d.get("customer") or 0, agency=request.agency)
    lst = get_object_or_404(Listing, pk=d.get("listing") or 0, company=request.agency, status=Listing.Status.ACTIVE)
    o = order_svc.create_order(
        user=c.user, listing=lst, unit_id=d.get("unit") or None, plan=d.get("plan") or Order.Plan.FULL,
        months=int(d.get("months") or 0), down_payment=d.get("down_payment") or None,
        delivery_method=d.get("delivery_method") or Order.Delivery.PICKUP, delivery_address=d.get("delivery_address", ""),
        buyer_name=d.get("buyer_name", ""), buyer_id_number=d.get("buyer_id_number", ""), by=request.user)
    order_svc.log_event(o, "NOTE", f"Sold by {request.user.full_name} ({request.agency.name}).", by=request.user)
    return Response(_order_row(request, _orders(request).get(pk=o.pk), detail=True), status=201)


# --------------------------------------------------------------------------- inquiries
def _inquiry_row(i: Inquiry) -> dict:
    return {**InquirySerializer(i).data, "customer": {"name": i.user.full_name, "phone": mask_phone(i.user.phone)},
            "listing_slug": i.listing.slug}


@company_api(["GET", "POST"])
def inquiries(request):
    """GET ``?status=OPEN|ANSWERED|CLOSED`` · POST ``{inquiry, reply}`` or ``{inquiry, action: "close"}``."""
    qs = Inquiry.objects.filter(listing__company=request.agency).select_related("listing", "user")
    if request.method == "POST":
        inq = get_object_or_404(qs, pk=request.data.get("inquiry") or 0)
        reply = str(request.data.get("reply") or "").strip()
        if reply:
            from apps.notifications.models import Notification
            from apps.notifications.services import notify

            from apps.chat import services as chat

            conv = chat.conversation_from_inquiry(inq) if not chat.Conversation.objects.filter(
                buyer=inq.user, listing=inq.listing, kind="SELLER").exists() else chat.for_listing(inq.user, inq.listing)
            msg = chat.post_message(conv, request.user, reply)  # screened like every chat message
            inq.reply, inq.status = msg.text[:2000], Inquiry.Status.ANSWERED
            inq.replied_by, inq.replied_at = request.user, timezone.now()
            inq.save()
        elif request.data.get("action") == "close":
            inq.status = Inquiry.Status.CLOSED
            inq.save(update_fields=["status", "updated_at"])
        else:
            raise BusinessRuleError("Write a reply.")
        return Response(_inquiry_row(inq))
    status = request.query_params.get("status")
    if status:
        qs = qs.filter(status=status.upper())
    return _paged(request, qs.order_by("-created_at"), _inquiry_row)


# --------------------------------------------------------------------------- customers
@company_api(["GET", "POST"])
def customers(request):
    if request.method == "POST":
        d = request.data
        cust, created = services.add_customer(
            request.agency, full_name=str(d.get("full_name") or "").strip(), phone=str(d.get("phone") or ""),
            email=str(d.get("email") or ""), password=str(d.get("pin") or ""), notes=str(d.get("notes") or ""),
            added_by=request.user)
        return Response({**_customer_row(cust), "created": created}, status=201)
    q = (request.query_params.get("q") or "").strip()
    qs = services.search_customers(request.agency, q).annotate(
        n_family=Count("family", distinct=True),
        n_orders=Count("user__orders", filter=Q(user__orders__company=request.agency,
                                                user__orders__status__in=[*Order.LIVE_STATUSES, Order.Status.COMPLETED]),
                       distinct=True),
        n_equb=Count("user__equb_memberships", filter=Q(user__equb_memberships__status__in=EqubMembership.LIVE_STATUSES),
                     distinct=True)).order_by("-created_at")
    return _paged(request, qs, _customer_row)


@company_api(["GET", "POST"])
def customer(request, pk: int):
    """GET detail · POST ``{action: set_pin|notes|add_family|remove_family, ...}``."""
    c = get_object_or_404(AgencyCustomer.objects.select_related("user"), pk=pk, agency=request.agency)
    if request.method == "POST":
        d, action = request.data, request.data.get("action")
        if action == "set_pin":
            services.set_customer_password(c, str(d.get("pin") or ""))
        elif action == "notes":
            c.notes = str(d.get("notes") or "")[:2000]
            c.save(update_fields=["notes", "updated_at"])
        elif action == "add_family":
            name = str(d.get("first_name") or "").strip()
            if not name:
                raise BusinessRuleError("Enter the family member's name.")
            from django.utils.dateparse import parse_date

            dob = parse_date(str(d.get("date_of_birth") or ""))
            if not dob or d.get("gender") not in FamilyMember.Gender.values:
                raise BusinessRuleError("Enter the family member's gender and date of birth (YYYY-MM-DD).")
            FamilyMember.objects.create(customer=c, first_name=name[:60], last_name=str(d.get("last_name") or "")[:60],
                                        relation=d.get("relation") or FamilyMember.Relation.OTHER, gender=d["gender"],
                                        date_of_birth=dob, phone=str(d.get("phone") or "")[:20])
        elif action == "remove_family":
            FamilyMember.objects.filter(pk=d.get("member"), customer=c).delete()
        else:
            raise BusinessRuleError("Unknown action.")
    places = list(EqubMembership.objects.filter(user=c.user).exclude(status=EqubMembership.Status.LEFT)
                  .select_related("group", "user", "won_round").order_by("-created_at"))
    standings = standing_mod.standings_for(places) if places else {}
    purchases = _orders(request).filter(user=c.user).order_by("-created_at")[:20]
    relations = dict(FamilyMember._meta.get_field("relation").choices or [])
    return Response({**_customer_row(c),
                     "family": [{"id": fm.id, "name": fm.full_name, "relation": fm.relation,
                                 "relation_label": fm.get_relation_display(), "phone": fm.phone} for fm in c.family.all()],
                     "relations": [{"value": k, "label": v} for k, v in relations.items()],
                     "places": [_place_row(m, standings[m.id]) for m in places],
                     "purchases": [_order_row(request, o) for o in purchases]})


# --------------------------------------------------------------------------- Equb places
@company_api()
def equbs(request):
    """Equbs open for new members, and the places held by this company's customers."""
    groups = (EqubGroup.objects.filter(status__in=[EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE])
              .annotate(n=Count("memberships", filter=~Q(memberships__status=EqubMembership.Status.LEFT)))
              .order_by("status", "start_date"))
    places = list(EqubMembership.objects.filter(user__agency_link__agency=request.agency)
                  .exclude(status=EqubMembership.Status.LEFT).select_related("group", "user", "won_round")
                  .order_by("-created_at")[:200])
    standings = standing_mod.standings_for(places) if places else {}
    return Response({
        "groups": [{"name": g.name, "slug": g.slug, "status": g.status, "status_label": g.get_status_display(),
                    "contribution_amount": str(g.contribution_amount), "every": g.every, "members": g.n,
                    "max_members": g.max_members, "start_date": g.start_date, "is_full": g.n >= g.max_members,
                    "payout": str(g.payout_per_winner)} for g in groups],
        "places": [_place_row(m, standings[m.id]) for m in places],
        "collected": str(company_equb_cash(request.agency)["total"]),
    })


@company_api(["POST"])
def equb_enrol(request, slug: str):
    """``{customer, family_member?}`` – give a customer (or one of their family) a place."""
    g = get_object_or_404(EqubGroup, slug=slug, status__in=[EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE])
    c = get_object_or_404(AgencyCustomer.objects.select_related("user", "agency"), pk=request.data.get("customer") or 0,
                          agency=request.agency)
    fm = None
    if request.data.get("family_member"):
        fm = get_object_or_404(FamilyMember, pk=request.data["family_member"], customer=c)
    m = services.enrol(c, g, family_member=fm)
    return Response(_place_row(EqubMembership.objects.select_related("group", "user", "won_round").get(pk=m.pk)), status=201)


@company_api(["GET", "POST"])
def equb_place(request, pk: int):
    """GET what the place owes · POST ``{method, rounds | amount, reference?, note?}`` – money collected by the company."""
    m = get_object_or_404(EqubMembership.objects.select_related("group", "user", "won_round"), pk=pk,
                          user__agency_link__agency=request.agency)
    if request.method == "POST":
        d = request.data
        services.collect_equb_payment(request.agency, m, request.user, method=str(d.get("method") or ""),
                                      rounds=int(d["rounds"]) if d.get("rounds") else None,
                                      amount=d.get("amount") or None, reference=str(d.get("reference") or ""),
                                      note=str(d.get("note") or ""))
        m.refresh_from_db()
    plan = equb_svc.payment_plan(m)
    return Response({**_place_row(m), "plan": {"payable": plan["payable"], "owed": str(plan["owed"]),
                                               "options": plan["options"], "reason": plan.get("reason")},
                     "methods": {str(k): k.label for k in EqubPayment.OFFICE_METHODS},
                     "payments": [{"reference": p.reference, "amount": str(p.amount), "method": p.get_method_display(),
                                   "paid_at": p.paid_at or p.created_at, "by": p.recorded_by.full_name if p.recorded_by_id else ""}
                                  for p in m.payments.filter(status=EqubPayment.Status.PAID)
                                  .select_related("recorded_by").order_by("-created_at")[:20]]})


def company_equb_cash(agency) -> dict:
    """Equb money this company's staff collected from members – cash the company holds for Nebret."""
    qs = EqubPayment.objects.filter(status=EqubPayment.Status.PAID, recorded_by__agency_staff__agency=agency,
                                    method__in=EqubPayment.OFFICE_METHODS)
    agg = qs.aggregate(s=Sum("amount"), n=Count("id"))
    return {"total": agg["s"] or ZERO, "count": agg["n"] or 0, "payments": qs}


# --------------------------------------------------------------------------- earnings & profile
@company_api()
def earnings(request):
    a = request.agency
    qs = a.ledger.select_related("customer", "order").order_by("-created_at")
    status = request.query_params.get("payout")
    if status:
        qs = qs.filter(payout_status=status.upper())
    resp = _paged(request, qs, _ledger_row)
    cash = company_equb_cash(a)
    resp.data["summary"] = _summary(services.summary(a))
    resp.data["equb_cash"] = {"total": str(cash["total"]), "count": cash["count"]}
    from apps.payments.banks import commission_account

    acc = commission_account()
    # Where the company sends Nebret's commission (buyers pay the company directly).
    resp.data["commission_account"] = ({k: acc.get(k, "") for k in ("bank", "bank_name", "account_name",
                                                                     "account_number", "branch")} if acc else None)
    return resp


@company_api(["GET", "PATCH"])
def profile(request):
    a = request.agency
    if request.method == "PATCH":
        _need_manager(request)
        for field, limit in (("description", 4000), ("address", 255), ("city", 80), ("phone", 20), ("website", 200),
                             ("logo_url", 500), ("email", 254)):
            if field in request.data:
                setattr(a, field, str(request.data[field] or "")[:limit])
        a.save()
    return Response({**_company(request),
                     "team": [{"name": s.user.full_name, "phone": s.user.phone, "role": s.role,
                               "role_label": s.get_role_display(), "is_active": s.is_active}
                              for s in a.staff.select_related("user")]})
