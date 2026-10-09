"""Public catalogue routes.

These are the targets ``music.models.{Artist,Album,Track}.get_absolute_url``
reverse, so the URL names here are load-bearing: renaming one breaks every
model that points at it.
"""

from django.urls import path

from . import site_views

app_name = "music"

urlpatterns = [
    path("", site_views.HomeView.as_view(), name="home"),
    path("search/", site_views.SearchView.as_view(), name="search"),
    path("library/", site_views.LibraryView.as_view(), name="library"),
    path("player/manifest/", site_views.track_manifest, name="track-manifest"),
    path("player/collection/", site_views.collection_manifest, name="player-collection"),
    path("tracks/<slug:slug>/", site_views.TrackDetailView.as_view(), name="track-detail"),
    path("artists/<slug:slug>/", site_views.ArtistDetailView.as_view(), name="artist-detail"),
    path("albums/<slug:slug>/", site_views.AlbumDetailView.as_view(), name="album-detail"),
    path("genres/<slug:slug>/", site_views.GenreDetailView.as_view(), name="genre-detail"),
]
