from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.chat"
    verbose_name = "In-app chat"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.market.models import OrderEvent

        from .services import on_order_event

        post_save.connect(on_order_event, sender=OrderEvent, dispatch_uid="chat_order_cards")
