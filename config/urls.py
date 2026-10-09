"""Root URL configuration.

    /...              listener-facing site (home, tracks, artists, albums, playlists)
    /api/...          REST API (JWT protected)
    /dashboard/...    staff-only HTML dashboard
    /admin/...        Django admin
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from config.views import healthz

urlpatterns = [
    # First: a cheap, dependency-free probe the platform polls.
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("api/", include("config.api_urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path(
        "api/redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="redoc",
    ),
    path("dashboard/", include("dashboard.urls")),
    path("playlists/", include("playlists.urls")),
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(template_name="accounts/login.html"),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/", include("accounts.urls")),
    path("notifications/", include("notifications.urls")),
    # Last: `music.urls` owns "/" and the /tracks/ /artists/ /albums/ prefixes,
    # so it must not shadow the routes above.
    path("", include("music.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

admin.site.site_header = "AURALIS Control"
admin.site.site_title = "AURALIS Admin"
admin.site.index_title = "Catalogue operations"
