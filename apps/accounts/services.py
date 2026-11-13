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

from .models import User

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
