from django.contrib import admin

from .models import BankReference, FraudAlert, PaymentTransaction, WebhookEvent


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ["reference", "user", "provider", "purpose", "amount", "status", "fulfilled", "created_at"]
    list_filter = ["provider", "purpose", "status", "fulfilled"]
    search_fields = ["reference", "provider_reference", "user__phone", "payer_phone"]
    readonly_fields = [f.name for f in PaymentTransaction._meta.fields]
    date_hierarchy = "created_at"


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ["provider", "signature_valid", "transaction", "created_at"]
    list_filter = ["provider", "signature_valid"]
    readonly_fields = [f.name for f in WebhookEvent._meta.fields]


@admin.register(BankReference)
class BankReferenceAdmin(admin.ModelAdmin):
    list_display = ["bank", "reference", "payment", "source", "created_at"]
    list_filter = ["bank", "source"]
    search_fields = ["reference"]
    readonly_fields = [f.name for f in BankReference._meta.fields]


@admin.register(FraudAlert)
class FraudAlertAdmin(admin.ModelAdmin):
    list_display = ["created_at", "kind", "message", "agency", "order_reference", "resolved_at"]
    list_filter = ["kind", "resolved_at"]
    search_fields = ["message", "order_reference"]
