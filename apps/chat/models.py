"""
In-app chat between a buyer and a partner company (and Nebret support). It replaces phone calls and direct contact:
every message is screened server-side and contact details are masked (see :mod:`apps.chat.moderation`).

* One conversation per buyer + listing (linked to the order once one is placed); support conversations have no
  company. Any active staff member of the company can reply; Nebret staff can join any conversation.
* The original text of a masked message is kept for Nebret review only.
"""
from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class Conversation(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"
        BLOCKED = "BLOCKED", "Blocked by Nebret"

    class Kind(models.TextChoices):
        SELLER = "SELLER", "Buyer and seller"
        SUPPORT = "SUPPORT", "Nebret support"

    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.SELLER, db_index=True)
    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    agency = models.ForeignKey("agencies.Agency", null=True, blank=True, on_delete=models.CASCADE,
                               related_name="conversations")
    listing = models.ForeignKey("market.Listing", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="conversations")
    order = models.ForeignKey("market.Order", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="conversations")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True)
    last_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    support_joined = models.BooleanField(default=False, help_text="A Nebret staff member has joined")
    first_buyer_message_at = models.DateTimeField(null=True, blank=True)
    first_agency_reply_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_message_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["buyer", "listing"], name="uniq_conversation_buyer_listing",
                                               condition=models.Q(kind="SELLER", listing__isnull=False))]

    def __str__(self):
        return f"#{self.pk} {self.buyer} ↔ {self.agency or 'Nebret'}"


class Message(models.Model):
    class Role(models.TextChoices):
        BUYER = "BUYER", "Buyer"
        AGENCY = "AGENCY", "Company staff"
        NEBRET = "NEBRET", "Nebret"
        SYSTEM = "SYSTEM", "System"

    class Kind(models.TextChoices):
        TEXT = "TEXT", "Text"
        CARD = "CARD", "Structured card"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+")
    sender_role = models.CharField(max_length=6, choices=Role.choices)
    sender_label = models.CharField(max_length=160, blank=True, help_text="e.g. Sunshine Real Estate – Abebe (Sales)")
    kind = models.CharField(max_length=4, choices=Kind.choices, default=Kind.TEXT)
    text = models.TextField(blank=True, help_text="What participants see (contact details masked)")
    original_text = models.TextField(blank=True, help_text="Unmasked – Nebret review only")
    card = models.JSONField(default=dict, blank=True, help_text="Viewing request, order status, payment confirmed…")
    attachments = models.JSONField(default=list, blank=True, help_text="Private storage keys")
    was_masked = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.conversation_id}:{self.sender_role}: {self.text[:40]}"


class ReadState(models.Model):
    """Per participant read receipt."""

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="read_states")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    last_read_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["conversation", "user"], name="uniq_read_state")]


class Violation(models.Model):
    """Contact details someone tried to share in a message."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_violations")
    agency = models.ForeignKey("agencies.Agency", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="violations")
    kinds = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class Report(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        RESOLVED = "RESOLVED", "Resolved"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="reports")
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.OPEN, db_index=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    resolution = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Restriction(TimeStampedModel):
    """Moderation action on a user: a warning, a mute (can read, can't send) or a suspension (no chat at all)."""

    class Level(models.TextChoices):
        WARNED = "WARNED", "Warned"
        MUTED = "MUTED", "Muted"
        SUSPENDED = "SUSPENDED", "Suspended"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="chat_restrictions")
    level = models.CharField(max_length=10, choices=Level.choices)
    until = models.DateTimeField(null=True, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    lifted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
