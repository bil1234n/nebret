from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import BusinessRuleError
from apps.payments.serializers import PaymentTransactionSerializer

from .models import Category, Inquiry, Listing, ListingUnit, Order, SavedListing
from .serializers import (CategorySerializer, CompanySerializer, InquirySerializer, ListingDetailSerializer,
                          ListingListSerializer, OrderCreateSerializer, OrderDetailSerializer, OrderListSerializer,
                          OrderPayRequestSerializer, QuoteRequestSerializer)
from .services import handover, orders, pricing

ORDER_TABS = {
    "active": Q(status__in=Order.LIVE_STATUSES),
    "to_pay": Q(status=Order.Status.PENDING_PAYMENT),
    "completed": Q(status=Order.Status.COMPLETED),
    "closed": Q(status__in=[Order.Status.CANCELLED, Order.Status.EXPIRED, Order.Status.REFUNDED,
                            Order.Status.DEFAULTED]),
}


def order_tabs() -> dict:
    return ORDER_TABS


def _available_annotation(qs):
    return qs.annotate(_available=Count("units", filter=Q(units__status=ListingUnit.Status.AVAILABLE)))


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """Marketplace categories, grouped (VEHICLES · HOMES · LAND · ELECTRONICS)."""

    serializer_class = CategorySerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = "slug"
    pagination_class = None

    def get_queryset(self):
        qs = Category.objects.filter(is_active=True).annotate(
            listing_count=Count("listings", filter=Q(listings__status=Listing.Status.ACTIVE)))
        group = self.request.query_params.get("group")
        return qs.filter(group=group.upper()) if group else qs


class ListingViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Browse the marketplace.

    Filters: ``category`` (slug), ``group`` (VEHICLES / HOMES / LAND / ELECTRONICS), ``company`` (slug), ``city``,
    ``min_price``, ``max_price``, ``plan`` (FULL / RESERVATION / INSTALLMENT / EQUB_PAYOUT), ``condition``,
    ``featured=1``, ``q`` (search) and ``sort`` (newest · price · -price · popular).
    """

    permission_classes = [permissions.AllowAny]
    lookup_field = "slug"
    filterset_fields = []
    search_fields = []
    ordering_fields = []

    def get_serializer_class(self):
        return ListingDetailSerializer if self.action == "retrieve" else ListingListSerializer

    def get_queryset(self):
        p = self.request.query_params
        qs = Listing.objects.live() if self.action != "retrieve" else Listing.objects.exclude(
            status=Listing.Status.DRAFT)
        qs = _available_annotation(qs.select_related("category", "company").prefetch_related("images"))
        if p.get("category"):
            qs = qs.filter(category__slug=p["category"])
        if p.get("group"):
            qs = qs.filter(category__group=p["group"].upper())
        if p.get("company"):
            qs = qs.filter(company__slug=p["company"])
        if p.get("city"):
            qs = qs.filter(city__iexact=p["city"])
        if p.get("condition"):
            qs = qs.filter(condition=p["condition"].upper())
        if p.get("min_price"):
            qs = qs.filter(price__gte=p["min_price"])
        if p.get("max_price"):
            qs = qs.filter(price__lte=p["max_price"])
        plan = (p.get("plan") or "").upper()
        plan_filter = {"FULL": Q(allow_full_payment=True), "RESERVATION": Q(allow_reservation=True, reservation_fee__gt=0),
                       "INSTALLMENT": Q(allow_installments=True), "EQUB_PAYOUT": Q(allow_equb_payout=True)}.get(plan)
        if plan_filter is not None:
            qs = qs.filter(plan_filter)
        if p.get("featured") in ("1", "true"):
            qs = qs.filter(is_featured=True)
        if p.get("q"):
            q = p["q"].strip()
            qs = qs.filter(Q(title__icontains=q) | Q(brand__icontains=q) | Q(model_name__icontains=q)
                           | Q(summary__icontains=q) | Q(area__icontains=q) | Q(city__icontains=q)
                           | Q(category__name__icontains=q) | Q(company__name__icontains=q))
        sort = p.get("sort") or "newest"
        order = {"price": ["price"], "-price": ["-price"], "popular": ["-views", "-created_at"],
                 "newest": ["-is_featured", "-created_at"]}.get(sort, ["-is_featured", "-created_at"])
        return qs.order_by(*order)

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        user = self.request.user
        if user.is_authenticated:
            ctx["saved_ids"] = set(user.saved_listings.values_list("listing_id", flat=True))
        return ctx

    def retrieve(self, request, *args, **kwargs):
        listing = self.get_object()
        Listing.objects.filter(pk=listing.pk).update(views=F("views") + 1)
        return Response(self.get_serializer(listing).data)

    @action(detail=True, methods=["post"], permission_classes=[permissions.AllowAny])
    def quote(self, request, slug=None):
        listing = self.get_object()
        s = QuoteRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        unit = None
        if s.validated_data.get("unit"):
            unit = get_object_or_404(ListingUnit, pk=s.validated_data["unit"], listing=listing)
        q = pricing.quote(listing, unit=unit, plan=s.validated_data["plan"], months=s.validated_data.get("months") or 0,
                          down_payment=s.validated_data.get("down_payment"), delivery=s.validated_data["delivery"])
        return Response(q.as_dict())

    @action(detail=True, methods=["post", "delete"], permission_classes=[permissions.IsAuthenticated],
            url_path="save", url_name="save")
    def toggle_save(self, request, slug=None):
        listing = self.get_object()
        if request.method == "DELETE":
            SavedListing.objects.filter(user=request.user, listing=listing).delete()
            return Response({"saved": False})
        SavedListing.objects.get_or_create(user=request.user, listing=listing)
        return Response({"saved": True})

    @action(detail=False, methods=["get"], permission_classes=[permissions.IsAuthenticated])
    def saved(self, request):
        qs = _available_annotation(Listing.objects.filter(saves__user=request.user)
                                   .select_related("category", "company").prefetch_related("images"))
        return Response(ListingListSerializer(qs, many=True, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAuthenticated])
    def inquire(self, request, slug=None):
        listing = self.get_object()
        s = InquirySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        inquiry = s.save(listing=listing, user=request.user, phone=s.validated_data.get("phone") or request.user.phone)
        return Response(InquirySerializer(inquiry).data, status=status.HTTP_201_CREATED)


class CompanyViewSet(viewsets.ReadOnlyModelViewSet):
    """Verified partner companies: real-estate developers, car dealers, e-bike and electronics sellers."""

    serializer_class = CompanySerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = "slug"

    def get_queryset(self):
        from apps.agencies.models import Agency

        qs = Agency.objects.filter(status=Agency.Status.ACTIVE).annotate(
            listing_count=Count("listings", filter=Q(listings__status=Listing.Status.ACTIVE)))
        if self.request.query_params.get("kind"):
            qs = qs.filter(kind=self.request.query_params["kind"].upper())
        return qs.order_by("-is_verified", "name")


class InquiryViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = InquirySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Inquiry.objects.none()
        return Inquiry.objects.filter(user=self.request.user).select_related("listing")


class OrderViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """A customer's orders. ``?tab=active|to_pay|completed|closed``."""

    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "reference"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Order.objects.none()
        qs = (Order.objects.filter(user=self.request.user)
              .select_related("listing__category", "unit", "company").prefetch_related("installments"))
        tab = self.request.query_params.get("tab")
        if tab in ORDER_TABS:
            qs = qs.filter(ORDER_TABS[tab])
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return OrderCreateSerializer
        return OrderDetailSerializer if self.action == "retrieve" else OrderListSerializer

    def create(self, request, *args, **kwargs):
        s = OrderCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        key = d.pop("listing")
        listing = get_object_or_404(Listing, **({"pk": int(key)} if str(key).isdigit() else {"slug": key}))
        order = orders.create_order(user=request.user, listing=listing, unit_id=d.pop("unit", None), **d)
        return Response(OrderDetailSerializer(order, context={"request": request}).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def pay(self, request, reference=None):
        order = self.get_object()
        s = OrderPayRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        txn = orders.start_payment(order, request.user, count=s.validated_data.get("count") or 1,
                                   amount=s.validated_data.get("amount"),
                                   provider=s.validated_data.get("provider") or None,
                                   payer_phone=s.validated_data.get("payer_phone", ""))
        return Response(PaymentTransactionSerializer(txn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, reference=None):
        order = orders.cancel_order(self.get_object(), request.data.get("reason") or "Cancelled by customer")
        return Response(OrderDetailSerializer(order, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="use-payout")
    def use_payout(self, request, reference=None):
        """Spend an Equb payout (``voucher_code``) on this order."""
        from apps.equb.models import EqubPayout
        from apps.equb.services import redeem_payout

        order = self.get_object()
        code = (request.data.get("voucher_code") or "").strip().upper()
        payout = EqubPayout.objects.filter(voucher_code=code, membership__user=request.user).first()
        if not payout:
            raise BusinessRuleError("No payout with that voucher code was found on your account.")
        redeem_payout(payout, order)
        order.refresh_from_db()
        return Response(OrderDetailSerializer(order, context={"request": request}).data)


class HandoverVerifyView(APIView):
    """Staff / partner-company staff check a customer's handover pass (QR or reference)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from apps.agencies.services import staff_for

        member = staff_for(request.user)
        if not (request.user.is_staff or member):
            return Response({"detail": "Only staff can verify handover passes."}, status=403)
        order = handover.verify_pass(request.data.get("payload", ""))
        if not order or (member and not request.user.is_staff and order.company_id != member.agency_id):
            return Response({"valid": False, "detail": "This pass is not valid."}, status=404)
        return Response({"valid": True, "order": OrderDetailSerializer(order, context={"request": request}).data,
                         "checked_at": timezone.now()})
