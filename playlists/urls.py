"""Playlist HTML routes.

``playlists.models.Playlist.get_absolute_url`` returns the literal
``/playlists/<slug>/`` path, so the prefix below must stay in sync with it.

``new/`` is listed before ``<slug:slug>/`` so the literal route wins over the
catch-all; otherwise a playlist could never be named "new".
"""

from django.urls import path

from . import site_views

app_name = "playlists"

urlpatterns = [
    path("", site_views.PlaylistListView.as_view(), name="index"),
    path("new/", site_views.PlaylistCreateView.as_view(), name="create"),
    path("<slug:slug>/", site_views.PlaylistDetailView.as_view(), name="detail"),
    path("<slug:slug>/edit/", site_views.PlaylistUpdateView.as_view(), name="edit"),
    path("<slug:slug>/delete/", site_views.PlaylistDeleteView.as_view(), name="delete"),
    path("<slug:slug>/manage/", site_views.PlaylistManageView.as_view(), name="manage"),
]
