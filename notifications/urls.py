"""Notification HTML routes (mounted at /notifications/)."""

from django.urls import path

from . import site_views

app_name = "notifications"

urlpatterns = [
    path("", site_views.NotificationListView.as_view(), name="list"),
]
