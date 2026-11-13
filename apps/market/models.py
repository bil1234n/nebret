"""
Nebret Equb marketplace.

Partner companies (real-estate developers, car dealers, e-bike & electronics sellers) publish **listings** in a
**category**. Every sellable item is a **unit** (apartment 3B, a car with its VIN, one e-bike from a batch of 40), so
stock can never be oversold. A customer places an **order** for a unit and chooses how to pay:

* ``FULL``         – pay the whole price, the unit is sold.
* ``RESERVATION``  – pay a reservation fee to hold the unit, pay the balance before the reservation ends.
* ``INSTALLMENT``  – pay a down payment, then monthly **installments** (with a flat finance charge, if any).
* ``EQUB_PAYOUT``  – an Equb winner uses their payout as credit (any shortfall is paid as a normal payment).

Once fully paid the order waits for **handover** (keys, plates or the item itself); the customer shows a signed
handover pass that the company verifies at handover.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import TimeStampedModel
from apps.core.utils import generate_code

CENT = Decimal("0.01")
ZERO = Decimal("0")


def money(v) -> Decimal:
    return Decimal(v or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def _unique_slug(model, obj, text: str, fallback: str) -> str:
    base = slugify(text)[:120] or fallback
    slug, i = base, 2
    while model.objects.filter(slug=slug).exclude(pk=obj.pk).exists():
        slug, i = f"{base}-{i}", i + 1
    return slug


# =========================================================================== catalogue
class Category(models.Model):
    class Group(models.TextChoices):
        VEHICLES = "VEHICLES", "Cars & vehicles"
        HOMES = "HOMES", "Houses & apartments"
        LAND = "LAND", "Land & commercial"
        ELECTRONICS = "ELECTRONICS", "E-bikes & electronics"

    group = models.CharField(max_length=12, choices=Group.choices, db_index=True)
    name = models.CharField(max_length=80)
    name_am = models.CharField("Name (Amharic)", max_length=80, blank=True)
    name_ar = models.CharField("Name (Arabic)", max_length=80, blank=True)
    slug = models.SlugField(max_length=90, unique=True, blank=True)
    icon = models.CharField(max_length=40, blank=True, help_text="Ionicons name used by the app, e.g. car-sport")
    description = models.CharField(max_length=240, blank=True)
    spec_fields = models.JSONField(
        default=list, blank=True,
        help_text='Specs shown for listings in this category, e.g. [{"key": "bedrooms", "label": "Bedrooms"}, '
                  '{"key": "area_m2", "label": "Area", "unit": "m²"}]')
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Category, self, self.name, "category")
        super().save(*args, **kwargs)


class ListingQuerySet(models.QuerySet):
    def live(self):
        return self.filter(status=Listing.Status.ACTIVE, category__is_active=True).exclude(
            company__status="SUSPENDED")


class Listing(TimeStampedModel):
    """A product or property offered for sale (the catalogue page)."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft (hidden)"
        ACTIVE = "ACTIVE", "Live"
        SOLD_OUT = "SOLD_OUT", "Sold out"
        ARCHIVED = "ARCHIVED", "Archived"

    class Condition(models.TextChoices):
        NEW = "NEW", "Brand new"
        USED = "USED", "Used"
        OFF_PLAN = "OFF_PLAN", "Off-plan / under construction"
        RENOVATED = "RENOVATED", "Renovated"

    company = models.ForeignKey("agencies.Agency", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="listings", help_text="Partner company selling it (empty = Nebret Equb)")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="listings")
    title = models.CharField(max_length=140)
    slug = models.SlugField(max_length=160, unique=True, blank=True)
    summary = models.CharField(max_length=240)
    description = models.TextField(blank=True)
    brand = models.CharField(max_length=80, blank=True, help_text="Make / developer / manufacturer")
    model_name = models.CharField("Model / project", max_length=80, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Model year or year built / completion")
    condition = models.CharField(max_length=10, choices=Condition.choices, default=Condition.NEW)

    city = models.CharField(max_length=80, default="Addis Ababa", db_index=True)
    area = models.CharField("Sub-city / area", max_length=80, blank=True)
    address = models.CharField(max_length=200, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    specs = models.JSONField(default=dict, blank=True,
                             help_text='Key facts, e.g. {"bedrooms": 3, "area_m2": 145} or {"mileage_km": 0, '
                                       '"fuel": "Electric", "transmission": "Automatic"}')
    features = models.JSONField(default=list, blank=True, help_text='e.g. ["Parking", "Elevator", "24/7 security"]')
    image_url = models.URLField(max_length=500, blank=True, help_text="Cover picture")
    video_url = models.URLField(max_length=500, blank=True)

    price = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(0)])
    compare_at_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
                                           help_text="Old price shown crossed out (optional)")
    currency = models.CharField(max_length=3, default="ETB")

    # --- how it can be paid ----------------------------------------------------------------------
    allow_full_payment = models.BooleanField(default=True)
    allow_reservation = models.BooleanField(default=True)
    reservation_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                          help_text="Paid to hold a unit; it counts towards the price")
    reservation_days = models.PositiveSmallIntegerField(default=14, help_text="Days to pay the balance")
    allow_installments = models.BooleanField(default=False)
    min_down_payment_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("30"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))])
    max_installment_months = models.PositiveSmallIntegerField(default=12, validators=[MinValueValidator(1)])
    finance_rate_percent = models.DecimalField(
        "Finance charge (% per year, flat)", max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("60"))],
        help_text="0 = interest-free (Shariah-friendly). A flat charge on the financed amount, fixed up front.")
    allow_equb_payout = models.BooleanField(default=True, help_text="Equb winners may use their payout")

    # --- handover --------------------------------------------------------------------------------
    delivery_available = models.BooleanField(default=False)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    handover_days = models.PositiveSmallIntegerField(default=7, help_text="Days to hand over after full payment")
    warranty_months = models.PositiveSmallIntegerField(default=0)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    is_featured = models.BooleanField(default=False)
    views = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    objects = ListingQuerySet.as_manager()

    class Meta:
        ordering = ["-is_featured", "-created_at"]
        indexes = [models.Index(fields=["category", "status", "price"]), models.Index(fields=["city", "status"])]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(Listing, self, f"{self.brand} {self.title}".strip(), "listing")
        super().save(*args, **kwargs)

    def clean(self):
        if self.allow_installments and self.max_installment_months > settings.NEBRET["MAX_INSTALLMENT_MONTHS"]:
            raise ValidationError({"max_installment_months":
                                   f"At most {settings.NEBRET['MAX_INSTALLMENT_MONTHS']} months."})
        if self.reservation_fee and self.reservation_fee >= self.price:
            raise ValidationError({"reservation_fee": "The reservation fee must be less than the price."})
        if not any([self.allow_full_payment, self.allow_reservation, self.allow_installments, self.allow_equb_payout]):
            raise ValidationError("Allow at least one way to pay.")

    # --- stock -----------------------------------------------------------------------------------
    @property
    def units_available(self) -> int:
        if hasattr(self, "_available"):
            return self._available
        return self.units.filter(status=ListingUnit.Status.AVAILABLE).count()

    @property
    def is_buyable(self) -> bool:
        return self.status == self.Status.ACTIVE and self.units_available > 0

    @property
    def min_down_payment(self) -> Decimal:
        return money(self.price * self.min_down_payment_percent / 100)

    @property
    def seller_name(self) -> str:
        return self.company.name if self.company_id else "Nebret Equb"

    def payment_plans(self) -> list[str]:
        out = []
        if self.allow_full_payment:
            out.append(Order.Plan.FULL)
        if self.allow_reservation and self.reservation_fee > 0:
            out.append(Order.Plan.RESERVATION)
        if self.allow_installments:
            out.append(Order.Plan.INSTALLMENT)
        if self.allow_equb_payout:
            out.append(Order.Plan.EQUB_PAYOUT)
        return out

    def refresh_stock_status(self):
        """SOLD_OUT when nothing is left to sell, back to ACTIVE when a unit is released."""
        left = self.units.filter(status=ListingUnit.Status.AVAILABLE).exists()
        if self.status == self.Status.ACTIVE and not left:
            Listing.objects.filter(pk=self.pk).update(status=self.Status.SOLD_OUT)
        elif self.status == self.Status.SOLD_OUT and left:
            Listing.objects.filter(pk=self.pk).update(status=self.Status.ACTIVE)

    def add_units(self, count: int = 1, *, prefix: str = "") -> list["ListingUnit"]:
        start = self.units.count() + 1
        prefix = prefix or (self.slug[:6].upper().replace("-", "") or "UNIT")
        units = [ListingUnit(listing=self, code=f"{prefix}-{n:03d}", label=f"Unit {n}") for n in range(start, start + count)]
        created = ListingUnit.objects.bulk_create(units)
        self.refresh_stock_status()
        return created


class ListingImage(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to="listings/", blank=True)
    image_url = models.URLField(max_length=500, blank=True)
    caption = models.CharField(max_length=120, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.caption or f"Photo {self.pk}"


class ListingUnit(models.Model):
    """One sellable item: an apartment, a plot, a car (VIN), one e-bike of a batch."""

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        HELD = "HELD", "Held while paying"
        RESERVED = "RESERVED", "Reserved / being paid"
        SOLD = "SOLD", "Sold"
        BLOCKED = "BLOCKED", "Not for sale"

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="units")
    code = models.CharField(max_length=40, help_text="Stock code, VIN, plot or unit number")
    label = models.CharField(max_length=120, blank=True, help_text="e.g. Block A · Floor 3 · Unit 3B, or Pearl White")
    price_override = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
                                         help_text="Leave empty to use the listing price")
    specs = models.JSONField(default=dict, blank=True, help_text="Unit-specific facts (floor, colour, VIN…)")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.AVAILABLE, db_index=True)
    held_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="held_units")
    held_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["listing", "code"]
        constraints = [models.UniqueConstraint(fields=["listing", "code"], name="uniq_unit_code_per_listing")]

    def __str__(self):
        return f"{self.listing} – {self.label or self.code}"

    @property
    def price(self) -> Decimal:
        return self.price_override if self.price_override is not None else self.listing.price


class SavedListing(models.Model):
    """A customer's wishlist."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_listings")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="saves")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["user", "listing"], name="uniq_saved_listing")]


class Inquiry(TimeStampedModel):
    """A question or a viewing / test-drive request sent to the seller."""

    class Kind(models.TextChoices):
        QUESTION = "QUESTION", "Question"
        VIEWING = "VIEWING", "Site visit / viewing"
        TEST_DRIVE = "TEST_DRIVE", "Test drive"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        ANSWERED = "ANSWERED", "Answered"
        CLOSED = "CLOSED", "Closed"

    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="inquiries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="inquiries")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.QUESTION)
    message = models.TextField(max_length=1000)
    preferred_date = models.DateField(null=True, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True)
    reply = models.TextField(blank=True)
    replied_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    replied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "inquiries"

    def __str__(self):
        return f"{self.get_kind_display()} – {self.listing}"


# =========================================================================== orders
def _order_ref():
    return "NB" + generate_code(6)


class Order(TimeStampedModel):
    class Plan(models.TextChoices):
        FULL = "FULL", "Full payment"
        RESERVATION = "RESERVATION", "Reserve, then pay the balance"
        INSTALLMENT = "INSTALLMENT", "Down payment + monthly installments"
        EQUB_PAYOUT = "EQUB_PAYOUT", "Equb payout"

    class Status(models.TextChoices):
        PENDING_PAYMENT = "PENDING_PAYMENT", "Awaiting first payment"
        RESERVED = "RESERVED", "Reserved – balance due"
        INSTALLMENTS = "INSTALLMENTS", "Paying installments"
        PAID = "PAID", "Paid in full – awaiting handover"
        COMPLETED = "COMPLETED", "Handed over"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired (unpaid)"
        DEFAULTED = "DEFAULTED", "Defaulted"
        REFUNDED = "REFUNDED", "Refunded"

    OPEN_STATUSES = (Status.PENDING_PAYMENT, Status.RESERVED, Status.INSTALLMENTS, Status.PAID)
    LIVE_STATUSES = (Status.RESERVED, Status.INSTALLMENTS, Status.PAID)

    class Funding(models.TextChoices):
        DIRECT = "DIRECT", "Direct payment"
        EQUB_PAYOUT = "EQUB_PAYOUT", "Equb payout"
        LUCKY_DRAW = "LUCKY_DRAW", "Giveaway prize"

    class Delivery(models.TextChoices):
        PICKUP = "PICKUP", "Collect / handover at the seller"
        DELIVERY = "DELIVERY", "Deliver to my address"

    reference = models.CharField(max_length=10, unique=True, default=_order_ref, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    listing = models.ForeignKey(Listing, on_delete=models.PROTECT, related_name="orders")
    unit = models.ForeignKey(ListingUnit, null=True, blank=True, on_delete=models.PROTECT, related_name="orders")
    company = models.ForeignKey("agencies.Agency", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="orders", help_text="Seller (copied from the listing)")
    plan = models.CharField(max_length=12, choices=Plan.choices, default=Plan.FULL)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING_PAYMENT, db_index=True)

    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    finance_charge = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, help_text="Everything the customer pays")
    first_payment = models.DecimalField(max_digits=14, decimal_places=2,
                                        help_text="Due now: full price, reservation fee or down payment")
    amount_paid = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    installment_months = models.PositiveSmallIntegerField(default=0)
    currency = models.CharField(max_length=3, default="ETB")
    price_breakdown = models.JSONField(default=dict, blank=True)

    funding_source = models.CharField(max_length=12, choices=Funding.choices, default=Funding.DIRECT)
    funding_reference = models.CharField(max_length=64, blank=True)
    delivery_method = models.CharField(max_length=8, choices=Delivery.choices, default=Delivery.PICKUP)
    delivery_address = models.CharField(max_length=255, blank=True)
    buyer_name = models.CharField(max_length=120, blank=True, help_text="Name on the title / contract")
    buyer_id_number = models.CharField("National ID / Kebele ID", max_length=40, blank=True)
    contact_phone = models.CharField(max_length=16)
    contact_email = models.EmailField(blank=True)
    note = models.CharField(max_length=255, blank=True, help_text="Note from the customer")

    expires_at = models.DateTimeField(null=True, blank=True, help_text="Unpaid orders are released at this time")
    reserved_until = models.DateField(null=True, blank=True, help_text="Reservation: pay the balance by this date")
    confirmed_at = models.DateTimeField(null=True, blank=True, help_text="First payment received")
    paid_in_full_at = models.DateTimeField(null=True, blank=True)
    handover_due = models.DateField(null=True, blank=True)
    handed_over_at = models.DateTimeField(null=True, blank=True)
    handed_over_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="+")
    handover_note = models.CharField(max_length=255, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+", help_text="Staff member who cancelled (empty = the customer)")
    refunded_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    refunded_at = models.DateTimeField(null=True, blank=True)
    refund_method = models.CharField(max_length=20, blank=True)
    refund_reference = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "status"]), models.Index(fields=["company", "status"])]

    def __str__(self):
        return f"{self.reference} ({self.get_status_display()})"

    # --- money -----------------------------------------------------------------------------------
    @property
    def balance_due(self) -> Decimal:
        return max(self.total_amount - self.amount_paid, ZERO)

    @property
    def financed_amount(self) -> Decimal:
        return max(self.total_amount - self.first_payment, ZERO)

    @property
    def progress_percent(self) -> int:
        if not self.total_amount:
            return 100
        return int(min(self.amount_paid / self.total_amount * 100, 100))

    @property
    def is_payable(self) -> bool:
        """The first payment can be made now."""
        return self.status == self.Status.PENDING_PAYMENT and (not self.expires_at or self.expires_at > timezone.now())

    @property
    def amount_due_now(self) -> Decimal:
        """What the "Pay" button collects right now."""
        if self.status == self.Status.PENDING_PAYMENT:
            return max(self.first_payment - self.amount_paid, ZERO)
        if self.status == self.Status.RESERVED:
            return self.balance_due
        if self.status == self.Status.INSTALLMENTS:
            nxt = self.next_installment
            return nxt.amount_due if nxt else ZERO
        return ZERO

    @property
    def next_installment(self):
        return self.installments.exclude(status__in=[Installment.Status.PAID, Installment.Status.WAIVED]).order_by(
            "number").first()

    @property
    def title(self) -> str:
        unit = f" · {self.unit.label}" if self.unit_id and self.unit.label else ""
        return f"{self.listing.title}{unit}"

    @property
    def refund_due(self) -> bool:
        return (self.status == self.Status.CANCELLED and self.amount_paid > 0
                and self.refunded_amount < self.amount_paid)


class Installment(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Upcoming"
        LATE = "LATE", "Late"
        PAID = "PAID", "Paid"
        WAIVED = "WAIVED", "Waived"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="installments")
    number = models.PositiveSmallIntegerField()
    due_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    late_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    paid_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING, db_index=True)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "number"]
        constraints = [models.UniqueConstraint(fields=["order", "number"], name="uniq_installment_number")]

    def __str__(self):
        return f"{self.order.reference} #{self.number}"

    @property
    def amount_due(self) -> Decimal:
        if self.status in (self.Status.PAID, self.Status.WAIVED):
            return ZERO
        return max(self.amount + self.late_fee - self.paid_amount, ZERO)

    @property
    def is_overdue(self) -> bool:
        grace = timedelta(days=settings.NEBRET["INSTALLMENT_GRACE_DAYS"])
        return self.amount_due > 0 and self.due_date + grace < timezone.localdate()


class OrderEvent(models.Model):
    """Audit trail for an order: what happened, when and who did it."""

    class Kind(models.TextChoices):
        CREATED = "CREATED", "Created"
        PAYMENT = "PAYMENT", "Payment"
        CONFIRMED = "CONFIRMED", "Confirmed"
        INSTALLMENT = "INSTALLMENT", "Installment"
        PAID_IN_FULL = "PAID_IN_FULL", "Paid in full"
        HANDED_OVER = "HANDED_OVER", "Handed over"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"
        REFUNDED = "REFUNDED", "Refunded"
        NOTE = "NOTE", "Note"
        MESSAGE = "MESSAGE", "Message sent"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    message = models.CharField(max_length=255)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.order.reference} {self.kind}: {self.message}"
