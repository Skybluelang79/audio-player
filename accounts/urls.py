"""Listener-facing account routes (profiles and the activity feed).

Password-reset pages live alongside these in ``accounts.auth_urls`` because they
are the same "account surfaces" concern.
"""

from django.urls import path

from . import auth_views, site_views

app_name = "accounts"

urlpatterns = [
    path("profile/edit/", site_views.ProfileEditView.as_view(), name="profile-edit"),
    path("profile/<uuid:pk>/", site_views.ProfileView.as_view(), name="profile"),
    path("feed/", site_views.FeedView.as_view(), name="feed"),
    path(
        "password-reset/",
        auth_views.ResetRequestView.as_view(),
        name="password-reset",
    ),
    path(
        "password-reset/done/",
        auth_views.ResetRequestDoneView.as_view(),
        name="password-reset-done",
    ),
    path(
        "password-reset/<uidb64>/<token>/",
        auth_views.ResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
    path(
        "password-reset/complete/",
        auth_views.ResetCompleteView.as_view(),
        name="password-reset-complete",
    ),
]
