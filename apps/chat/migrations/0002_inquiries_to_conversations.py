"""Existing inquiries become conversations (the request as a structured card, the seller's reply as a message)."""
from django.db import migrations


def forwards(apps, schema_editor):
    Inquiry = apps.get_model("market", "Inquiry")
    Conversation = apps.get_model("chat", "Conversation")
    Message = apps.get_model("chat", "Message")
    for inq in Inquiry.objects.select_related("listing", "user", "replied_by").order_by("created_at").iterator():
        conv, _ = Conversation.objects.get_or_create(
            kind="SELLER", buyer_id=inq.user_id, listing_id=inq.listing_id,
            defaults={"agency_id": inq.listing.company_id, "last_message_at": inq.created_at,
                      "first_buyer_message_at": inq.created_at})
        card = {}
        if inq.kind != "QUESTION":
            card = {"type": f"{inq.kind}_REQUEST", "preferred_date": inq.preferred_date.isoformat()
                    if inq.preferred_date else None, "inquiry": inq.pk}
        m = Message.objects.create(conversation=conv, sender_id=inq.user_id, sender_role="BUYER",
                                   sender_label=inq.user.full_name, kind="CARD" if card else "TEXT",
                                   text=inq.message, card=card)
        Message.objects.filter(pk=m.pk).update(created_at=inq.created_at)
        last = inq.created_at
        if inq.reply:
            label = inq.listing.company.name if inq.listing.company_id else "Nebret"
            r = Message.objects.create(conversation=conv, sender_id=inq.replied_by_id, sender_role="AGENCY",
                                       sender_label=label, text=inq.reply)
            when = inq.replied_at or inq.updated_at
            Message.objects.filter(pk=r.pk).update(created_at=when)
            last = when
            if not conv.first_agency_reply_at:
                conv.first_agency_reply_at = when
        conv.last_message_at = max(conv.last_message_at or last, last)
        conv.save()


class Migration(migrations.Migration):
    dependencies = [("chat", "0001_initial"), ("market", "0002_bank_transfers")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
