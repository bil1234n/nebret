import re

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.utils import generate_code

ETH_PHONE_RE = re.compile(r"^\+2519\d{8}$|^\+2517\d{8}$")


def normalize_phone(raw: str) -> str:
    """Normalise Ethiopian numbers to E.164 (+2519XXXXXXXX)."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "+251" + digits[1:]
    elif digits.startswith("251"):
        digits = "+" + digits
    elif len(digits) == 9 and digits[0] in "79":
        digits = "+251" + digits
    return digits


def validate_phone(value: str):
    if not ETH_PHONE_RE.match(value):
        raise ValidationError("Enter a valid Ethiopian mobile number, e.g. +251911234567.")


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, phone, password, **extra):
        if not phone:
            raise ValueError("Phone number is required")
        phone = normalize_phone(phone)
        user = self.model(phone=phone, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, phone, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(phone, password, **extra)

    def create_superuser(self, phone, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True)
        return self._create_user(phone, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    class DeletionStatus(models.TextChoices):
        NONE = "", "Active"
        PENDING = "PENDING", "Deletion requested (suspended)"
        DELETED = "DELETED", "Deleted"

    class Language(models.TextChoices):
        AMHARIC = "am", "Amharic"
        ENGLISH = "en", "English"
        OROMO = "om", "Afaan Oromo"
        ARABIC = "ar", "Arabic"

    phone = models.CharField(max_length=16, unique=True, validators=[validate_phone])
    email = models.EmailField(blank=True)
    full_name = models.CharField(max_length=150)
    preferred_language = models.CharField(max_length=2, choices=Language.choices, default=Language.ENGLISH)

    # Buyer details (used to pre-fill orders and sale contracts)
    national_id = models.CharField("National ID / Kebele ID", max_length=32, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    city = models.CharField(max_length=80, blank=True)
    address = models.CharField(max_length=255, blank=True, help_text="Default delivery address")

    # Viral acquisition
    referral_code = models.CharField(max_length=8, unique=True, editable=False)
    referred_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals")

    push_token = models.CharField(max_length=255, blank=True)
    is_phone_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    # Account deletion (Google Play requires it): the customer asks, the account is suspended at once,
    # staff approve (personal data is wiped) or restore it. See apps/accounts/services.py.
    deletion_status = models.CharField(max_length=10, choices=DeletionStatus.choices, default=DeletionStatus.NONE,
                                       blank=True, db_index=True)
    deletion_reason = models.TextField(blank=True)
    deletion_requested_at = models.DateTimeField(null=True, blank=True)
    deletion_reviewed_at = models.DateTimeField(null=True, blank=True)
    deletion_reviewed_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    deletion_note = models.CharField(max_length=255, blank=True, help_text="Staff note on the decision")

    objects = UserManager()

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self):
        return f"{self.full_name} ({self.phone})"

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        if not self.referral_code:
            code = generate_code(7)
            while User.objects.filter(referral_code=code).exists():
                code = generate_code(7)
            self.referral_code = code
        super().save(*args, **kwargs)

    @property
    def first_name(self):
        return self.full_name.split(" ")[0] if self.full_name else ""

    @property
    def deletion_pending(self) -> bool:
        return self.deletion_status == self.DeletionStatus.PENDING


PIN_LENGTH = 6


def validate_pin(value: str) -> str:
    """App sign-in uses a numeric PIN (like telebirr), typed on the app's own keypad."""
    from django.core.exceptions import ValidationError

    value = (value or "").strip()
    if not (value.isdigit() and len(value) == PIN_LENGTH):
        raise ValidationError(f"The PIN must be exactly {PIN_LENGTH} digits.")
    if len(set(value)) == 1 or value in ("123456", "654321", "012345"):
        raise ValidationError("That PIN is too easy to guess. Choose another one.")
    return value


class KycProfile(models.Model):
    """
    Know-your-member details an Equb member gives before they can win a draw: their photo, national ID and
    monthly income. Pictures are kept in the database (small JPEGs) so they survive on read-only hosts.
    """

    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Submitted"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected – send again"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="kyc")
    photo = models.BinaryField(editable=False)
    id_photo = models.BinaryField(null=True, blank=True, editable=False)
    national_id = models.CharField("National ID number", max_length=32)
    monthly_income = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUBMITTED, db_index=True)
    review_note = models.CharField(max_length=255, blank=True)
    reviewed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "member profile (KYC)"
        verbose_name_plural = "member profiles (KYC)"

    def __str__(self):
        return f"{self.user} – {self.get_status_display()}"

    @property
    def is_valid(self) -> bool:
        return self.status != self.Status.REJECTED


def kyc_done(user) -> bool:
    profile = KycProfile.objects.filter(user=user).only("status").first()
    return bool(profile and profile.is_valid)
