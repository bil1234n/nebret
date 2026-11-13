"""
Partner companies on Nebret Equb (the app label stays ``agencies``).

A company – a real-estate developer, a car dealer, an e-bike or electronics seller – publishes its own listings in the
marketplace, takes orders for them (full payment, reservation, installments or Equb payouts), signs up its own
customers (and their family members), enrols them in Equbs and hands the property over when it is paid.

Every sale is split: the company keeps the sale, Nebret Equb keeps ``commission_percent`` (5 % by default). With a
Chapa subaccount, Chapa settles the company's share directly to the company's bank account at payment time; otherwise
the share is recorded as owed and paid out from the console.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

from apps.core.models import TimeStampedModel

CENT = Decimal("0.01")


def default_commission():
    return Decimal(str(settings.NEBRET.get("AGENCY_COMMISSION_PERCENT", 5)))


class Agency(TimeStampedModel):
    """A partner company (seller)."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending approval"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"

    class Kind(models.TextChoices):
        REAL_ESTATE = "REAL_ESTATE", "Real-estate developer"
        CAR_DEALER = "CAR_DEALER", "Car dealer / importer"
        EBIKE = "EBIKE", "E-bike & motorbike seller"
        ELECTRONICS = "ELECTRONICS", "Electronics & appliances"
        LAND = "LAND", "Land & commercial property"
        OTHER = "OTHER", "Other"

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160, unique=True, blank=True)
    kind = models.CharField("Business type", max_length=12, choices=Kind.choices, default=Kind.REAL_ESTATE,
                            db_index=True)
    description = models.TextField(blank=True, help_text="Shown to customers on the company page")
    logo_url = models.URLField(max_length=500, blank=True)
    website = models.URLField(blank=True)
    is_verified = models.BooleanField(default=False, help_text="Licence and documents checked by Nebret Equb")
    license_number = models.CharField(max_length=60, blank=True, help_text="Trade licence number")
    tin = models.CharField("TIN", max_length=20, blank=True)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    city = models.CharField(max_length=80, default="Addis Ababa")
    address = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=default_commission,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("50"))],
        help_text="Nebret Equb's commission on every sale, taken from the company's share.")

    # Payout account → Chapa subaccount (Chapa sends the company's share here automatically).
    bank_code = models.PositiveIntegerField(null=True, blank=True, help_text="Chapa bank id")
    bank_name = models.CharField(max_length=80, blank=True)
    account_name = models.CharField(max_length=120, blank=True)
    account_number = models.CharField(max_length=40, blank=True)
    chapa_subaccount_id = models.CharField(max_length=80, blank=True,
                                           help_text="Set automatically when the Chapa subaccount is created")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    notes = models.TextField(blank=True, help_text="Internal notes (only Nebret Equb staff see these)")

    class Meta:
        ordering = ["name"]
        verbose_name = "partner company"
        verbose_name_plural = "partner companies"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:140] or "company"
            slug, i = base, 2
            while Agency.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, i = f"{base}-{i}", i + 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def has_split(self) -> bool:
        return bool(self.chapa_subaccount_id)

    def commission_on(self, amount) -> Decimal:
        return (Decimal(amount) * self.commission_percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)


class AgencyStaff(TimeStampedModel):
    """A person who works for a company and signs in to the company portal."""

    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        MANAGER = "MANAGER", "Manager"
        AGENT = "AGENT", "Sales agent"

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="staff")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agency_staff")
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.AGENT)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["agency", "role", "user__full_name"]
        verbose_name = "company staff member"
        verbose_name_plural = "company staff"

    def __str__(self):
        return f"{self.user.full_name} – {self.agency} ({self.get_role_display()})"

    @property
    def can_manage(self) -> bool:
        return self.role in (self.Role.OWNER, self.Role.MANAGER)


class AgencyCustomer(TimeStampedModel):
    """A customer the company signed up. A customer belongs to one company."""

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="customers")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="agency_link")
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "company customer"

    def __str__(self):
        return f"{self.user.full_name} @ {self.agency}"


class FamilyMember(TimeStampedModel):
    """
    A relative of a company customer (often without a phone): a co-buyer or beneficiary on an order, or an Equb place
    as a responsibility person that the customer pays for.
    """

    class Relation(models.TextChoices):
        SPOUSE = "SPOUSE", "Spouse"
        CHILD = "CHILD", "Child"
        PARENT = "PARENT", "Parent"
        SIBLING = "SIBLING", "Brother / sister"
        OTHER = "OTHER", "Other relative"

    class Gender(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"

    customer = models.ForeignKey(AgencyCustomer, on_delete=models.CASCADE, related_name="family")
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    relation = models.CharField(max_length=8, choices=Relation.choices, default=Relation.OTHER)
    gender = models.CharField(max_length=1, choices=Gender.choices)
    date_of_birth = models.DateField()
    phone = models.CharField(max_length=20, blank=True)
    national_id = models.CharField("National ID / Kebele ID", max_length=40, blank=True)

    class Meta:
        ordering = ["customer", "first_name"]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class AgencyLedgerEntry(TimeStampedModel):
    """
    One company sale: what the customer paid, Nebret Equb's commission and the company's share.

    * ``GATEWAY`` – paid through a gateway or at the office. With a Chapa subaccount the company's share was settled
      by Chapa; otherwise it is owed and paid out by Nebret Equb.
    * ``CREDIT``  – an order paid with an Equb payout or a prize (money Nebret Equb already holds) – owed.
    """

    class Kind(models.TextChoices):
        GATEWAY = "GATEWAY", "Paid online / at the office"
        CREDIT = "CREDIT", "Paid with Equb payout / prize credit"

    class Payout(models.TextChoices):
        SETTLED_BY_CHAPA = "SETTLED_BY_CHAPA", "Sent to company by Chapa"
        OWED = "OWED", "Owed to company"
        PAID = "PAID", "Paid to company"
        REVERSED = "REVERSED", "Reversed (refund)"

    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="ledger")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    order = models.ForeignKey("market.Order", null=True, blank=True, on_delete=models.PROTECT,
                              related_name="agency_entries")
    payment = models.OneToOneField("payments.PaymentTransaction", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="agency_entry")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    gross_amount = models.DecimalField(max_digits=14, decimal_places=2)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    commission = models.DecimalField(max_digits=14, decimal_places=2)
    agency_amount = models.DecimalField(max_digits=14, decimal_places=2)
    payout_status = models.CharField(max_length=16, choices=Payout.choices, default=Payout.OWED, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    paid_reference = models.CharField(max_length=64, blank=True)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "company ledger entry"
        verbose_name_plural = "company ledger entries"

    def __str__(self):
        return f"{self.agency} {self.gross_amount} ({self.get_payout_status_display()})"
