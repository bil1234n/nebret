from django.urls import path

from . import views

app_name = "agency"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("logout/", views.logout_view, name="logout"),
    path("listings/", views.listings, name="listings"),
    path("listings/new/", views.listing_form, name="listing_add"),
    path("listings/<int:pk>/", views.listing_detail, name="listing"),
    path("listings/<int:pk>/edit/", views.listing_form, name="listing_edit"),
    path("orders/", views.orders, name="orders"),
    path("orders/<str:reference>/", views.order_detail, name="order"),
    path("sell/", views.sell, name="sell"),
    path("handover/", views.handover_desk, name="handover"),
    path("inquiries/", views.inquiries, name="inquiries"),
    path("chats/", views.chats, name="chats"),
    path("chats/<int:pk>/", views.chat_detail, name="chat"),
    path("customers/", views.customers, name="customers"),
    path("customers/new/", views.customer_add, name="customer_add"),
    path("customers/<int:pk>/", views.customer_detail, name="customer"),
    path("customers/<int:pk>/family/new/", views.family_form, name="family_add"),
    path("customers/<int:pk>/family/<int:member>/", views.family_form, name="family_edit"),
    path("equb/", views.equbs, name="equbs"),
    path("equb/<slug:slug>/enrol/", views.equb_enrol, name="equb_enrol"),
    path("equb/place/<int:pk>/", views.equb_place, name="equb_place"),
    path("earnings/", views.earnings, name="earnings"),
    path("team/", views.team, name="team"),
    path("profile/", views.profile, name="profile"),
]
