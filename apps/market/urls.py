from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CategoryViewSet, CompanyViewSet, HandoverVerifyView, InquiryViewSet, ListingViewSet, OrderViewSet

router = DefaultRouter()
router.register("categories", CategoryViewSet, basename="category")
router.register("listings", ListingViewSet, basename="listing")
router.register("companies", CompanyViewSet, basename="company")
router.register("orders", OrderViewSet, basename="order")
router.register("inquiries", InquiryViewSet, basename="inquiry")

urlpatterns = [path("handover/verify/", HandoverVerifyView.as_view(), name="handover-verify")] + router.urls
