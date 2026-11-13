from decimal import Decimal

from rest_framework import serializers

from .models import Category, Inquiry, Installment, Listing, ListingImage, ListingUnit, Order, OrderEvent, money
from .services import handover


def _abs(request, url: str) -> str:
    if not url:
        return ""
    return url if url.startswith("http") or request is None else request.build_absolute_uri(url)


class CategorySerializer(serializers.ModelSerializer):
    group_label = serializers.CharField(source="get_group_display", read_only=True)
    listing_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Category
        fields = ["id", "group", "group_label", "name", "name_am", "name_ar", "slug", "icon", "description",
                  "spec_fields", "listing_count"]


class CompanyMiniSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    slug = serializers.CharField()
    kind = serializers.CharField()
    kind_label = serializers.CharField(source="get_kind_display")
    logo_url = serializers.CharField()
    city = serializers.CharField()
    phone = serializers.CharField()
    is_verified = serializers.BooleanField()


class CompanySerializer(CompanyMiniSerializer):
    description = serializers.CharField()
    address = serializers.CharField()
    website = serializers.CharField()
    email = serializers.CharField()
    listing_count = serializers.IntegerField(default=0)


def _monthly_from(listing: Listing) -> str | None:
    if not listing.allow_installments or not listing.max_installment_months:
        return None
    months = listing.max_installment_months
    financed = listing.price - listing.min_down_payment
    charge = financed * listing.finance_rate_percent / 100 * months / 12
    return str(money((financed + charge) / months))


def _spec_rows(listing: Listing) -> list[dict]:
    """Specs with their labels, in the order the category defines (unknown keys follow)."""
    specs = listing.specs or {}
    fields = listing.category.spec_fields or []
    rows, seen = [], set()
    for f in fields:
        key = f.get("key")
        if key in specs and specs[key] not in (None, ""):
            rows.append({"key": key, "label": f.get("label", key), "value": specs[key], "unit": f.get("unit", "")})
            seen.add(key)
    for key, value in specs.items():
        if key not in seen and value not in (None, ""):
            rows.append({"key": key, "label": key.replace("_", " ").capitalize(), "value": value, "unit": ""})
    return rows


class ListingImageSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = ListingImage
        fields = ["id", "url", "caption"]

    def get_url(self, obj) -> str:
        return _abs(self.context.get("request"), obj.image.url if obj.image else obj.image_url)


class UnitSerializer(serializers.ModelSerializer):
    price = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = ListingUnit
        fields = ["id", "code", "label", "price", "specs", "status"]


class ListingListSerializer(serializers.ModelSerializer):
    category = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()
    condition_label = serializers.CharField(source="get_condition_display", read_only=True)
    units_available = serializers.IntegerField(read_only=True)
    payment_plans = serializers.SerializerMethodField()
    monthly_from = serializers.SerializerMethodField()
    key_specs = serializers.SerializerMethodField()
    seller_name = serializers.CharField(read_only=True)
    is_saved = serializers.SerializerMethodField()
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Listing
        fields = ["id", "slug", "title", "summary", "category", "brand", "model_name", "year", "condition",
                  "condition_label", "city", "area", "price", "compare_at_price", "currency", "image_url",
                  "is_featured", "status", "units_available", "payment_plans", "min_down_payment",
                  "max_installment_months", "monthly_from", "key_specs", "company", "seller_name", "is_saved"]

    def get_category(self, obj) -> dict:
        c = obj.category
        return {"slug": c.slug, "name": c.name, "group": c.group, "icon": c.icon}

    def get_company(self, obj) -> dict | None:
        return CompanyMiniSerializer(obj.company).data if obj.company_id else None

    def get_payment_plans(self, obj) -> list[str]:
        return [str(p) for p in obj.payment_plans()]

    def get_monthly_from(self, obj) -> str | None:
        return _monthly_from(obj)

    def get_key_specs(self, obj) -> list[dict]:
        return _spec_rows(obj)[:3]

    def get_is_saved(self, obj) -> bool:
        saved = self.context.get("saved_ids")
        if saved is not None:
            return obj.id in saved
        user = getattr(self.context.get("request"), "user", None)
        return bool(user and user.is_authenticated and obj.saves.filter(user=user).exists())

    def get_image_url(self, obj) -> str:
        request = self.context.get("request")
        if obj.image_url:
            return obj.image_url
        first = obj.images.first()
        return _abs(request, (first.image.url if first.image else first.image_url)) if first else ""


class ListingDetailSerializer(ListingListSerializer):
    specs = serializers.SerializerMethodField()
    images = ListingImageSerializer(many=True, read_only=True)
    units = serializers.SerializerMethodField()
    company = serializers.SerializerMethodField()

    class Meta(ListingListSerializer.Meta):
        fields = ListingListSerializer.Meta.fields + [
            "description", "specs", "features", "images", "video_url", "address", "latitude", "longitude",
            "allow_full_payment", "allow_reservation", "reservation_fee", "reservation_days", "allow_installments",
            "min_down_payment_percent", "finance_rate_percent", "allow_equb_payout", "delivery_available",
            "delivery_fee", "handover_days", "warranty_months", "units", "views"]

    def get_specs(self, obj) -> list[dict]:
        return _spec_rows(obj)

    def get_units(self, obj) -> list[dict]:
        return UnitSerializer(obj.units.filter(status=ListingUnit.Status.AVAILABLE)[:200], many=True).data

    def get_company(self, obj) -> dict | None:
        return CompanySerializer(obj.company).data if obj.company_id else None


class QuoteRequestSerializer(serializers.Serializer):
    plan = serializers.ChoiceField(choices=Order.Plan.choices, default=Order.Plan.FULL)
    unit = serializers.IntegerField(required=False, allow_null=True)
    months = serializers.IntegerField(required=False, min_value=1, max_value=120)
    down_payment = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    delivery = serializers.BooleanField(default=False)


class OrderCreateSerializer(serializers.Serializer):
    listing = serializers.CharField(help_text="Listing slug or id")
    unit = serializers.IntegerField(required=False, allow_null=True)
    plan = serializers.ChoiceField(choices=Order.Plan.choices, default=Order.Plan.FULL)
    months = serializers.IntegerField(required=False, min_value=1, max_value=120)
    down_payment = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True)
    delivery_method = serializers.ChoiceField(choices=Order.Delivery.choices, default=Order.Delivery.PICKUP)
    delivery_address = serializers.CharField(required=False, allow_blank=True, max_length=255)
    buyer_name = serializers.CharField(required=False, allow_blank=True, max_length=120)
    buyer_id_number = serializers.CharField(required=False, allow_blank=True, max_length=40)
    contact_phone = serializers.CharField(required=False, allow_blank=True, max_length=16)
    contact_email = serializers.EmailField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)


class InstallmentSerializer(serializers.ModelSerializer):
    amount_due = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Installment
        fields = ["id", "number", "due_date", "amount", "late_fee", "paid_amount", "amount_due", "status",
                  "status_label", "is_overdue", "paid_at"]


class OrderEventSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = OrderEvent
        fields = ["kind", "kind_label", "message", "created_at"]


class OrderListSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    plan_label = serializers.CharField(source="get_plan_display", read_only=True)
    listing = serializers.SerializerMethodField()
    unit_label = serializers.SerializerMethodField()
    seller_name = serializers.SerializerMethodField()
    balance_due = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    amount_due_now = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    progress_percent = serializers.IntegerField(read_only=True)
    next_installment = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["reference", "status", "status_label", "plan", "plan_label", "listing", "unit_label", "seller_name",
                  "total_amount", "amount_paid", "balance_due", "amount_due_now", "progress_percent", "currency",
                  "next_installment", "expires_at", "reserved_until", "handover_due", "created_at"]

    def get_listing(self, obj) -> dict:
        lst = obj.listing
        image = lst.image_url
        if not image:
            first = lst.images.first()
            image = _abs(self.context.get("request"), (first.image.url if first.image else first.image_url)) if first else ""
        return {"slug": lst.slug, "title": lst.title, "image_url": image, "group": lst.category.group,
                "category": lst.category.name, "city": lst.city}

    def get_unit_label(self, obj) -> str:
        return (obj.unit.label or obj.unit.code) if obj.unit_id else ""

    def get_seller_name(self, obj) -> str:
        return obj.company.name if obj.company_id else "Nebret"

    def get_next_installment(self, obj) -> dict | None:
        if obj.status != Order.Status.INSTALLMENTS:
            return None
        nxt = obj.next_installment
        return InstallmentSerializer(nxt).data if nxt else None


class OrderDetailSerializer(OrderListSerializer):
    installments = InstallmentSerializer(many=True, read_only=True)
    events = serializers.SerializerMethodField()
    handover_pass = serializers.SerializerMethodField()
    cancellation = serializers.SerializerMethodField()
    installment_options = serializers.SerializerMethodField()
    funding_source_label = serializers.CharField(source="get_funding_source_display", read_only=True)
    delivery_method_label = serializers.CharField(source="get_delivery_method_display", read_only=True)
    financed_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    company = serializers.SerializerMethodField()

    class Meta(OrderListSerializer.Meta):
        fields = OrderListSerializer.Meta.fields + [
            "unit_price", "delivery_fee", "finance_charge", "discount_amount", "first_payment", "financed_amount",
            "installment_months", "price_breakdown", "funding_source", "funding_source_label", "funding_reference",
            "delivery_method", "delivery_method_label", "delivery_address", "buyer_name", "buyer_id_number",
            "contact_phone", "contact_email", "note", "confirmed_at", "paid_in_full_at", "handed_over_at",
            "cancelled_at", "cancellation_reason", "refunded_amount", "installments", "events", "handover_pass",
            "cancellation", "installment_options", "company"]

    def get_events(self, obj) -> list[dict]:
        public = [k for k in OrderEvent.Kind.values if k not in (OrderEvent.Kind.NOTE,)]
        return OrderEventSerializer(obj.events.filter(kind__in=public)[:30], many=True).data

    def get_handover_pass(self, obj) -> dict | None:
        if obj.status not in (Order.Status.PAID, Order.Status.COMPLETED):
            return None
        return {"payload": handover.pass_payload(obj), "code": handover.signature(obj),
                "used": obj.status == Order.Status.COMPLETED}

    def get_cancellation(self, obj) -> dict:
        from .services.orders import cancellation_policy

        return cancellation_policy(obj)

    def get_installment_options(self, obj) -> list[dict]:
        from .services.orders import installment_options

        return installment_options(obj) if obj.status == Order.Status.INSTALLMENTS else []

    def get_company(self, obj) -> dict | None:
        return CompanySerializer(obj.company).data if obj.company_id else None


class OrderPayRequestSerializer(serializers.Serializer):
    count = serializers.IntegerField(required=False, min_value=1, default=1, help_text="Installments to pay")
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, required=False, allow_null=True,
                                      help_text="Reservation: pay part of the balance")
    provider = serializers.CharField(required=False, allow_blank=True)
    payer_phone = serializers.CharField(required=False, allow_blank=True, max_length=16)


class InquirySerializer(serializers.ModelSerializer):
    listing_title = serializers.CharField(source="listing.title", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Inquiry
        fields = ["id", "listing", "listing_title", "kind", "message", "preferred_date", "phone", "status",
                  "status_label", "reply", "replied_at", "created_at"]
        read_only_fields = ["listing", "status", "reply", "replied_at", "created_at"]


ZERO = Decimal("0")
