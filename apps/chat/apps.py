from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.chat"
    verbose_name = "In-app chat"

    def ready(self):
        from django.db.models.signals import post_save, pre_save

        from apps.market.models import Listing, OrderEvent

        from .services import _listing_post_save, _listing_pre_save, on_order_event

        post_save.connect(on_order_event, sender=OrderEvent, dispatch_uid="chat_order_cards")
        # a listing that changes seller takes its buyer chats with it
        pre_save.connect(_listing_pre_save, sender=Listing, dispatch_uid="chat_listing_seller_pre")
        post_save.connect(_listing_post_save, sender=Listing, dispatch_uid="chat_listing_seller_post")
