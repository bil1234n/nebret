from django.apps import AppConfig


class MarketConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.market"
    verbose_name = "Marketplace"

    def ready(self):
        from apps.payments import registry

        from .services.orders import on_order_paid, on_order_payment_failed

        registry.register("ORDER", on_success=on_order_paid, on_failure=on_order_payment_failed)
        registry.register("INSTALLMENT", on_success=on_order_paid, on_failure=on_order_payment_failed)

        from django.db.models.signals import post_save

        from .models import Listing

        post_save.connect(_translate_listing, sender=Listing, dispatch_uid="translate_listing")


def _translate_listing(sender, instance, **kwargs):
    """Translate a saved product into every app language (after the save is committed)."""
    from django.db import transaction

    from apps.core import translation

    if translation.enabled():
        transaction.on_commit(lambda: translation.warm_listing(instance))
