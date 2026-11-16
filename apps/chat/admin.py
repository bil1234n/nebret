from django.contrib import admin

from .models import Conversation, Message, Report, Restriction, Violation


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ["id", "kind", "buyer", "agency", "listing", "order", "status", "last_message_at"]
    list_filter = ["kind", "status"]
    search_fields = ["buyer__full_name", "agency__name", "listing__title", "order__reference"]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ["id", "conversation", "sender_role", "sender_label", "was_masked", "created_at"]
    list_filter = ["sender_role", "was_masked"]


admin.site.register(Report)
admin.site.register(Restriction)
admin.site.register(Violation)
