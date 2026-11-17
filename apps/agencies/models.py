"""
Partner companies on Nebret (the app label stays ``agencies``).

A company – a real-estate developer, a car dealer, an e-bike or electronics seller – publishes its own listings in the
marketplace, takes orders for them (full payment, reservation, installments or Equb payouts), signs up its own
customers (and their family members), enrols them in Equbs and hands the property over when it is paid.

Every sale is split: the company keeps the sale, Nebret keeps ``commission_percent`` (5 % by default). With a
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
    is_verified = models.BooleanField(default=False, help_text="Licence and documents checked by Nebret")
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
        help_text="Nebret's commission on every sale, taken from the company's share.")

    # Payout account → Chapa subaccount (Chapa sends the company's share here automatically).
    bank_code = models.PositiveIntegerField(null=True, blank=True, help_text="Chapa bank id")
    bank_name = models.CharField(max_length=80, blank=True)
    account_name = models.CharField(max_length=120, blank=True)
    account_number = models.CharField(max_length=40, blank=True)
    chapa_subaccount_id = models.CharField(max_length=80, blank=True,
                                           help_text="Set automatically when the Chapa subaccount is created")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    notes = models.TextField(blank=True, help_text="Internal notes (only Nebret staff see these)")

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
    One company sale: what the customer paid, Nebret's commission and the company's share.

    * ``GATEWAY`` – paid through a gateway or at the office. With a Chapa subaccount the company's share was settled
      by Chapa; otherwise it is owed and paid out by Nebret.
    * ``CREDIT``  – an order paid with an Equb payout or a prize (money Nebret already holds) – owed.

    ``direction`` says who owes whom:

    * ``TO_COMPANY`` – Nebret holds the money and owes the company ``agency_amount``
      (OWED → PAID, or SETTLED_BY_CHAPA when Chapa paid it directly).
    * ``TO_NEBRET``  – the money went straight to the company (into its own bank account or its till), so the company
      owes Nebret ``commission`` (RECEIVABLE → INVOICED → COLLECTED, or WRITTEN_OFF).
    """

    class Direction(models.TextChoices):
        TO_COMPANY = "TO_COMPANY", "Nebret owes the company its share"
        TO_NEBRET = "TO_NEBRET", "Company owes Nebret its commission"

    class Kind(models.TextChoices):
        GATEWAY = "GATEWAY", "Paid online / at the office"
        CREDIT = "CREDIT", "Paid with Equb payout / prize credit"

    class Payout(models.TextChoices):
        SETTLED_BY_CHAPA = "SETTLED_BY_CHAPA", "Sent to company by Chapa"
        OWED = "OWED", "Owed to company"
        PAID = "PAID", "Paid to company"
        REVERSED = "REVERSED", "Reversed (refund)"
        # Commission the company owes Nebret (direction TO_NEBRET)
        RECEIVABLE = "RECEIVABLE", "Commission due from company"
        INVOICED = "INVOICED", "Commission invoiced"
        COLLECTED = "COLLECTED", "Commission collected"
        WRITTEN_OFF = "WRITTEN_OFF", "Commission written off"

    COMPANY_OWED = (Payout.OWED,)
    NEBRET_RECEIVABLE = (Payout.RECEIVABLE, Payout.INVOICED)

    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="ledger")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    direction = models.CharField(max_length=10, choices=Direction.choices, default=Direction.TO_COMPANY,
                                 db_index=True)
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


class PartnerBankAccount(TimeStampedModel):
    """
    A bank account the company is paid into. Buyers transfer straight into it – Nebret never holds marketplace money.

    Only the company OWNER can submit a new or changed account; Nebret staff approve it, and it becomes usable after a
    cooling-off period (``BANK_ACCOUNT_COOLING_OFF_HOURS``) so a hijacked owner login can't redirect payments
    instantly. Accounts are never edited in place: a change is a new account that replaces the old one when approved.
    Every step is kept in :class:`BankAccountEvent`.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for Nebret approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        RETIRED = "RETIRED", "Replaced / switched off"

    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="bank_accounts")
    bank = models.CharField(max_length=10)  # apps.payments.banks.Bank
    account_name = models.CharField(max_length=120)
    account_number = models.CharField(max_length=40)
    currency = models.CharField(max_length=3, default="ETB")
    swift_code = models.CharField(max_length=11, blank=True)
    branch = models.CharField(max_length=80, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    is_active = models.BooleanField(default=False, help_text="Shown to buyers (after approval and cooling-off)")
    is_verified = models.BooleanField(default=False, help_text="Checked and approved by Nebret staff")
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    usable_from = models.DateTimeField(null=True, blank=True, help_text="End of the cooling-off period")
    replaces = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    rejection_reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        ordering = ["agency", "-created_at"]
        verbose_name = "partner bank account"

    def __str__(self):
        return f"{self.agency} · {self.bank} {self.masked_number}"

    @property
    def masked_number(self) -> str:
        n = self.account_number
        return ("•" * max(len(n) - 4, 0)) + n[-4:]

    @property
    def is_usable(self) -> bool:
        from django.utils import timezone

        return (self.status == self.Status.APPROVED and self.is_active and self.is_verified
                and self.usable_from is not None and self.usable_from <= timezone.now())

    def snapshot(self) -> dict:
        """What the buyer is told to pay into – frozen on the order."""
        from apps.payments.banks import Bank

        return {"account_id": self.pk, "bank": self.bank, "bank_name": dict(Bank.choices).get(self.bank, self.bank),
                "account_name": self.account_name, "account_number": self.account_number, "currency": self.currency,
                "swift_code": self.swift_code, "branch": self.branch, "agency_id": self.agency_id,
                "agency_name": self.agency.name}


class BankAccountEvent(models.Model):
    """Full change history of a company's bank accounts."""

    account = models.ForeignKey(PartnerBankAccount, on_delete=models.CASCADE, related_name="events")
    action = models.CharField(max_length=20)  # SUBMITTED · APPROVED · REJECTED · ACTIVATED · RETIRED
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=255, blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.account} {self.action}"


class CommissionPayment(TimeStampedModel):
    """
    The company paid Nebret's commission by bank transfer into Nebret's commission account and uploaded the receipt.
    ShegerPay checks it (like a buyer's receipt); once verified the oldest commission due is marked collected.
    Any amount beyond what was due is kept as ``unallocated`` credit and used for the next commission.
    """

    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Checking with the bank"
        VERIFIED = "VERIFIED", "Verified – commission collected"
        MANUAL = "MANUAL", "Nebret is checking"
        REJECTED = "REJECTED", "Rejected"

    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="commission_payments")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    amount_verified = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    unallocated = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    bank = models.CharField(max_length=16)
    reference = models.CharField(max_length=64, blank=True)
    transfer_datetime = models.DateTimeField()
    payer_name = models.CharField(max_length=120, blank=True)
    file = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUBMITTED, db_index=True)
    reason = models.CharField(max_length=255, blank=True)
    bank_check = models.JSONField(default=dict, blank=True)
    verify_attempts = models.PositiveIntegerField(default=0)
    next_check_at = models.DateTimeField(null=True, blank=True)
    paid_to = models.JSONField(default=dict, blank=True, help_text="Nebret's account at the time")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    entries = models.ManyToManyField(AgencyLedgerEntry, blank=True, related_name="commission_payments")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.agency} commission {self.amount} ({self.status})"

    @property
    def display_reference(self) -> str:
        return self.reference or "read from receipt…"
