from django.urls import path

from . import api

urlpatterns = [
    path("dashboard/", api.dashboard),
    path("listings/", api.listings),
    path("listings/<int:pk>/", api.listing),
    path("listings/<int:pk>/equbs/", api.listing_equbs),
    path("orders/", api.orders),
    path("orders/<str:reference>/", api.order),
    path("handover/check/", api.handover_check),
    path("payments/", api.payments),
    path("payments/<int:pk>/", api.payment),
    path("bank-accounts/", api.bank_accounts),
    path("bank-accounts/<int:pk>/", api.bank_account),
    path("sell/", api.sell),
    path("inquiries/", api.inquiries),
    path("customers/", api.customers),
    path("customers/<int:pk>/", api.customer),
    path("equbs/", api.equbs),
    path("equbs/<slug:slug>/enrol/", api.equb_enrol),
    path("equb-places/<int:pk>/", api.equb_place),
    path("earnings/", api.earnings),
    path("profile/", api.profile),
]
