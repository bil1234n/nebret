from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.accounts.web import delete_account, privacy_policy, terms_of_service
from apps.core.views import AppVersionView, BannerListView, DashboardView, HealthView, LandingView, private_file, cron_tick
from apps.market.web import verify_agreement
from apps.luckydraw.web import give

admin.site.site_header = "Nebret Equb Administration"
admin.site.site_title = "Nebret Equb Admin"

urlpatterns = [
    path("", LandingView.as_view(), name="landing"),
    path("privacy/", privacy_policy, name="privacy"),
    path("terms/", terms_of_service, name="terms"),
    path("account/delete/", delete_account, name="delete-account-web"),
    path("give/<slug:slug>/", give, name="give-web"),
    path("agreements/verify/<str:reference>/<int:version>/<str:signature>/", verify_agreement,
         name="agreement-verify"),
    path("admin/", admin.site.urls),
    path("console/", include("apps.console.urls")),
    path("company/", include("apps.agencies.urls")),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/home/dashboard/", DashboardView.as_view(), name="dashboard"),
    path("api/home/banners/", BannerListView.as_view(), name="banners"),
    path("api/app/version/", AppVersionView.as_view(), name="app-version"),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/market/", include("apps.market.urls")),
    path("api/company/", include("apps.agencies.api_urls")),
    path("api/equb/", include("apps.equb.urls")),
    path("api/community/", include("apps.luckydraw.urls")),
    path("api/payments/", include("apps.payments.urls")),
    path("api/notifications/", include("apps.notifications.urls")),
    path("api/chat/", include("apps.chat.urls")),
    path("api/files/<str:token>/", private_file, name="private-file"),
    path("api/cron/tick/", cron_tick, name="cron-tick"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
