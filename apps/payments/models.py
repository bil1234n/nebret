import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.core.models import TimeStampedModel


class PaymentTransaction(TimeStampedModel):
    """A single attempt to collect Birr from a user through an aggregator."""

    class Provider(models.TextChoices):
        TELEBIRR = "telebirr", "telebirr"
        CHAPA = "chapa", "Chapa"
        MOCK = "mock", "Sandbox (mock)"
        # Recorded by staff at the office (no gateway involved)
        CASH = "cash", "Cash (office)"
        BANK = "bank", "Bank transfer"
        MOBILE = "mobile", "Mobile money (manual)"

    class Purpose(models.TextChoices):
        ORDER = "ORDER", "Marketplace order (full payment / deposit)"
        INSTALLMENT = "INSTALLMENT", "Marketplace installment"
        EQUB_CONTRIBUTION = "EQUB_CONTRIBUTION", "Equb contribution"
        DONATION = "DONATION", "Crowdfunding donation"
        MICRO_DONATION = "MICRO_DONATION", "Old lucky-draw entry (retired)"

    class Status(models.TextChoices):
        INITIATED = "INITIATED", "Initiated"
        PENDING = "PENDING", "Awaiting customer"
        SUCCESS = "SUCCESS", "Successful"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"
        REFUNDED = "REFUNDED", "Refunded"

    FINAL_STATUSES = {Status.SUCCESS, Status.FAILED, Status.CANCELLED, Status.EXPIRED, Status.REFUNDED}
    OFFICE_PROVIDERS = {Provider.CASH, Provider.BANK, Provider.MOBILE}

    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=16, choices=Provider.choices)
    purpose = models.CharField(max_length=24, choices=Purpose.choices, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="ETB")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INITIATED, db_index=True)

    # What is being paid for (Order, Installment, Contribution, Donation…)
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    provider_reference = models.CharField(max_length=128, blank=True, db_index=True)
    checkout_url = models.URLField(max_length=1024, blank=True)
    payer_phone = models.CharField(max_length=16, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    raw_request = models.JSONField(default=dict, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    callback_payload = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    fulfilled = models.BooleanField(default=False, help_text="Business side-effects have been applied.")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+", help_text="Staff member who recorded an office payment")
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Partner-company sales: the company keeps the rest, Nebret keeps ``platform_fee``.
    agency = models.ForeignKey("agencies.Agency", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="payments", help_text="Partner company this sale belongs to")
    platform_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                       help_text="Nebret's commission on a company sale")
    split_settled = models.BooleanField(default=False,
                                        help_text="Chapa paid the company's share directly (split payment)")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return f"{self.get_provider_display()} {self.amount} {self.currency} [{self.status}]"

    @property
    def is_final(self):
        return self.status in self.FINAL_STATUSES

    @property
    def is_office(self):
        return self.provider in self.OFFICE_PROVIDERS


class WebhookEvent(TimeStampedModel):
    """Raw inbound callbacks — kept for audit, replay and dispute handling."""

    provider = models.CharField(max_length=16)
    headers = models.JSONField(default=dict)
    body = models.TextField()
    signature_valid = models.BooleanField(default=False)
    transaction = models.ForeignKey(PaymentTransaction, null=True, blank=True, on_delete=models.SET_NULL, related_name="webhooks")
    processing_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class BankReference(models.Model):
    """
    Every bank / mobile-money transfer reference that has been used to pay for something, across all orders and all
    companies. The unique constraint is the last line of defence against one receipt being used twice (two orders,
    two companies, or the same order twice) – checks in code run first so the user gets a clear message.

    ``bank`` is a bank code (e.g. ``CBE``), ``BANK`` when the bank wasn't recorded (old office payments) or
    ``MOBILE`` for mobile money. ``reference`` is normalised: uppercase, letters and digits only.
    """

    UNKNOWN_BANK = "BANK"
    MOBILE = "MOBILE"

    bank = models.CharField(max_length=20)
    reference = models.CharField(max_length=64)
    payment = models.ForeignKey(PaymentTransaction, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="bank_references")
    source = models.CharField(max_length=20, default="office", help_text="office · receipt · backfill")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["bank", "reference"], name="uniq_bank_reference")]
        indexes = [models.Index(fields=["reference"])]

    def __str__(self):
        return f"{self.bank}:{self.reference}"


class FraudAlert(TimeStampedModel):
    """
    The Nebret staff alert queue: a reused transfer reference, repeated rejected receipts, verifier outages, disputes,
    overdue seller confirmations or refunds, contact details shared in chat…
    """

    class Kind(models.TextChoices):
        DUPLICATE_REFERENCE = "DUPLICATE_REFERENCE", "Transfer reference used twice"
        REPEATED_REJECTIONS = "REPEATED_REJECTIONS", "Receipts rejected again and again"
        VERIFIER_OUTAGE = "VERIFIER_OUTAGE", "Receipt could not be verified for hours"
        MANUAL_REVIEW = "MANUAL_REVIEW", "Receipt needs manual review"
        DISPUTE = "DISPUTE", "Seller's statement doesn't match the receipt"
        PARTNER_SLA = "PARTNER_SLA", "Seller hasn't confirmed a receipt in time"
        REFUND_OVERDUE = "REFUND_OVERDUE", "Seller refund overdue"
        CHAT_VIOLATIONS = "CHAT_VIOLATIONS", "Contact details shared in chat"
        EQUB_ORDER_FAILED = "EQUB_ORDER_FAILED", "Product Equb win could not create the order"

    kind = models.CharField(max_length=24, choices=Kind.choices, db_index=True)
    message = models.CharField(max_length=255)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+", help_text="Who submitted / recorded it")
    proof_id = models.PositiveBigIntegerField(null=True, blank=True, db_index=True)
    agency = models.ForeignKey("agencies.Agency", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    order_reference = models.CharField(max_length=20, blank=True, db_index=True)
    existing = models.ForeignKey(BankReference, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                 help_text="The earlier use of the reference")
    data = models.JSONField(default=dict, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    resolution_note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.message}"
