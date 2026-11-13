from django.apps import AppConfig


class AgenciesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agencies"
    verbose_name = "Partner companies"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.market.models import Order

        from .services import on_order_saved

        post_save.connect(on_order_saved, sender=Order, dispatch_uid="company_credit_sale")
