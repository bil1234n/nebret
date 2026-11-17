from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    """Chats about a listing that has since changed seller move to the current seller (with a note in the chat)."""
    Conversation = apps.get_model("chat", "Conversation")
    Message = apps.get_model("chat", "Message")
    for conv in Conversation.objects.filter(kind="SELLER", listing__isnull=False).select_related("listing__company"):
        listing = conv.listing
        if conv.agency_id == listing.company_id:
            continue
        seller = listing.company.name if listing.company_id else "Nebret"
        now = timezone.now()
        conv.agency_id = listing.company_id
        conv.first_agency_reply_at = None
        conv.last_message_at = now
        conv.save(update_fields=["agency", "first_agency_reply_at", "last_message_at", "updated_at"])
        Message.objects.create(conversation=conv, sender=None, sender_role="SYSTEM", sender_label="Nebret", kind="CARD",
                               text=f"{listing.title} is now sold by {seller}. This chat continues with them.",
                               card={"type": "SELLER_CHANGED", "seller": seller})


class Migration(migrations.Migration):
    dependencies = [("chat", "0002_inquiries_to_conversations")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
