"""Playlist HTML routes.

``playlists.models.Playlist.get_absolute_url`` returns the literal
``/playlists/<slug>/`` path, so the prefix below must stay in sync with it.
"""

from django.urls import path

from . import site_views

app_name = "playlists"

urlpatterns = [
    path("", site_views.PlaylistListView.as_view(), name="index"),
    path("<slug:slug>/", site_views.PlaylistDetailView.as_view(), name="detail"),
]
