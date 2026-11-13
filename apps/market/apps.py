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
