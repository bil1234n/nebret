"""
Account deletion – required by Google Play for apps that let people create an account.

1. ``request_deletion`` – the customer asks (in the app or on the public web page) and gives a reason.
   The account is suspended straight away: they can no longer sign in and nothing is sent to them.
2. Staff review the request in the console (Customers → Deletion requests) and either
   * ``approve_deletion`` – personal data is wiped. Payments, orders and Equb records are kept (anonymised)
     because they are financial records the business must keep; they no longer point to a person, or
   * ``restore_account`` – the request is declined and the customer can sign in again.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import KycProfile, User

REASON_MAX = 1000


def deletion_blockers(user: User) -> dict:
    """What staff should settle before approving: live Equb places, orders being paid and pending handovers."""
    from apps.equb.models import EqubMembership
    from apps.market.models import Order

    live = EqubMembership.objects.filter(user=user, status__in=EqubMembership.LIVE_STATUSES)
    orders = Order.objects.filter(user=user)
    return {"live_equbs": live.count(),
            "paying_orders": orders.filter(status__in=[Order.Status.RESERVED, Order.Status.INSTALLMENTS]).count(),
            "pending_handovers": orders.filter(status=Order.Status.PAID).count(),
            "unpaid_orders": orders.filter(status=Order.Status.PENDING_PAYMENT).count()}


@transaction.atomic
def request_deletion(user: User, reason: str) -> User:
    """Suspend the account and queue it for staff review. Calling it again only updates the reason."""
    if user.deletion_status == User.DeletionStatus.DELETED:
        return user
    user.deletion_status = User.DeletionStatus.PENDING
    user.deletion_reason = (reason or "").strip()[:REASON_MAX]
    user.deletion_requested_at = user.deletion_requested_at or timezone.now()
    user.deletion_reviewed_at = None
    user.deletion_reviewed_by = None
    user.deletion_note = ""
    user.is_active = False          # blocks sign-in and every API call with an old token
    user.push_token = ""            # no more push notifications
    user.save(update_fields=["deletion_status", "deletion_reason", "deletion_requested_at", "deletion_reviewed_at",
                             "deletion_reviewed_by", "deletion_note", "is_active", "push_token"])
    return user


@transaction.atomic
def restore_account(user: User, by: User | None = None, note: str = "") -> User:
    """Decline the request: the customer can sign in again."""
    if user.deletion_status != User.DeletionStatus.PENDING:
        return user
    user.deletion_status = User.DeletionStatus.NONE
    user.deletion_reviewed_at = timezone.now()
    user.deletion_reviewed_by = by
    user.deletion_note = (note or "Request declined – account restored")[:255]
    user.is_active = True
    user.save(update_fields=["deletion_status", "deletion_reviewed_at", "deletion_reviewed_by", "deletion_note",
                             "is_active"])
    return user


@transaction.atomic
def approve_deletion(user: User, by: User | None = None, note: str = "") -> User:
    """
    Wipe the person's data. Financial records (payments, orders, Equb contributions) stay for accounting but are
    anonymised: the account keeps no name, phone, email, ID or address, and the phone number is freed so it
    can register again.
    """
    from apps.market.models import Inquiry, Order
    from apps.notifications.models import Notification

    if user.deletion_status == User.DeletionStatus.DELETED:
        return user
    Order.objects.filter(user=user).update(contact_phone="", contact_email="", buyer_id_number="",
                                           delivery_address="", note="")
    Inquiry.objects.filter(user=user).update(phone="", message="(deleted)")
    user.saved_listings.all().delete()
    KycProfile.objects.filter(user=user).delete()
    Notification.objects.filter(user=user).delete()

    user.full_name = "Deleted user"
    user.email = ""
    user.national_id = ""
    user.date_of_birth = None
    user.city = ""
    user.address = ""
    user.push_token = ""
    user.phone = f"+000{user.pk:012d}"   # unique placeholder (16 chars) – frees the real number
    user.set_unusable_password()
    user.is_active = False
    user.deletion_status = User.DeletionStatus.DELETED
    user.deletion_reviewed_at = timezone.now()
    user.deletion_reviewed_by = by
    user.deletion_note = (note or "Approved – personal data deleted")[:255]
    user.save()
    return user


# --------------------------------------------------------------------------- member profile (KYC)
MAX_UPLOAD = 8 * 1024 * 1024


def _jpeg(upload, side: int = 1100) -> bytes:
    """Any phone picture → a small JPEG (EXIF rotation applied), so it is cheap to store and show."""
    import io

    from PIL import Image, ImageOps

    from apps.core.exceptions import BusinessRuleError

    if upload is None:
        raise BusinessRuleError("Add the picture.")
    if getattr(upload, "size", 0) > MAX_UPLOAD:
        raise BusinessRuleError("That picture is too large. Choose one under 8 MB.")
    try:
        img = ImageOps.exif_transpose(Image.open(upload))
        img = img.convert("RGB")
    except Exception:
        raise BusinessRuleError("That file is not a picture we can read. Take the photo again.")
    img.thumbnail((side, side))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=78, optimize=True)
    return out.getvalue()


def submit_kyc(user, *, photo, national_id: str, monthly_income, id_photo=None):
    from decimal import Decimal, InvalidOperation

    from apps.core.exceptions import BusinessRuleError

    from .models import KycProfile

    national_id = (national_id or "").strip()
    if len(national_id) < 4:
        raise BusinessRuleError("Enter your national ID number.")
    try:
        income = Decimal(str(monthly_income or "0"))
    except InvalidOperation:
        raise BusinessRuleError("Enter your monthly income as a number.")
    if income <= 0:
        raise BusinessRuleError("Enter your monthly income.")
    existing = KycProfile.objects.filter(user=user).first()
    fields = {"national_id": national_id[:32], "monthly_income": income, "status": KycProfile.Status.SUBMITTED,
              "review_note": "", "reviewed_by": None, "reviewed_at": None, "submitted_at": timezone.now()}
    if photo is not None or existing is None:
        fields["photo"] = _jpeg(photo)
    if id_photo is not None:
        fields["id_photo"] = _jpeg(id_photo, side=1400)
    profile, _ = KycProfile.objects.update_or_create(user=user, defaults=fields)
    if user.national_id != profile.national_id:
        user.national_id = profile.national_id
        user.save(update_fields=["national_id"])
    return profile


def review_kyc(profile, staff, *, approve: bool, note: str = ""):
    from .models import KycProfile

    profile.status = KycProfile.Status.APPROVED if approve else KycProfile.Status.REJECTED
    profile.review_note = (note or "")[:255]
    profile.reviewed_by, profile.reviewed_at = staff, timezone.now()
    profile.save(update_fields=["status", "review_note", "reviewed_by", "reviewed_at"])
    if not approve:
        from apps.notifications.models import Notification
        from apps.notifications.services import notify

        notify(profile.user, Notification.Kind.EQUB_REMINDER, "Please send your details again",
               "We could not accept your photo or ID" + (f": {profile.review_note}" if profile.review_note else ".")
               + " Open your Equb and fill the form again to stay in the draws.", {"kyc": 1})
    return profile
