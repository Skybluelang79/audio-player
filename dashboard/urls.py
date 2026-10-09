"""Staff dashboard routes."""

from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.DashboardHome.as_view(), name="index"),
    path("data/", views.DashboardDataView.as_view(), name="data"),
    path("catalogue/", views.CatalogueView.as_view(), name="catalogue"),
    path("catalogue/bulk/", views.BulkCatalogueView.as_view(), name="bulk"),
    path(
        "catalogue/bulk/actions/",
        views.BulkActionView.as_view(),
        name="bulk-action",
    ),
    path(
        "catalogue/export.csv",
        views.CatalogueExportView.as_view(),
        name="catalogue-export",
    ),
    path("audience/", views.AudienceView.as_view(), name="audience"),
    path("upload/", views.QuickUploadView.as_view(), name="upload"),
    path("moderation/", views.ModerationQueueView.as_view(), name="moderation"),
    path(
        "moderation/actions/",
        views.ModerationActionView.as_view(),
        name="moderation-action",
    ),
    path("tracks/<int:pk>/", views.TrackAnalyticsView.as_view(), name="track-analytics"),
]
