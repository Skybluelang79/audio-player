"""Listener-facing HTML views for playlists.

Visibility is enforced through ``Playlist.is_visible_to`` rather than by
filtering a queryset, so a private playlist returns a real 404 to a stranger
and its slug cannot be probed.
"""

from django.http import Http404
from django.shortcuts import get_object_or_404
from django.views.generic import DetailView, ListView

from music.site_views import visible_playlists

from .models import Playlist


def human_duration(seconds) -> str:
    """``3725`` -> ``"1 hr 2 min"``. Matches ``Playlist.duration_display``."""
    total = int(seconds or 0)
    hours, minutes = divmod(total // 60, 60)
    return f"{hours} hr {minutes} min" if hours else f"{minutes} min"


class PlaylistDetailView(DetailView):
    model = Playlist
    template_name = "playlists/detail.html"
    context_object_name = "playlist"
    slug_field = "slug"

    def get_object(self, queryset=None):
        # Deliberately not DetailView's default: the visibility rule needs the
        # request, and it must apply to unlisted playlists too (reachable by
        # direct link, never listed).
        playlist = get_object_or_404(
            Playlist.objects.select_related("owner").prefetch_related("collaborators"),
            slug=self.kwargs.get("slug"),
        )
        if not playlist.is_visible_to(self.request.user):
            raise Http404("No playlist matches the given query.")
        return playlist

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        playlist = self.object
        user = self.request.user

        # `playlist_tracks` is the ordered source of truth; a plain `tracks`
        # queryset would lose the position column and shuffle the listing.
        entries = list(
            playlist.playlist_tracks.select_related("track__artist", "track__album").order_by(
                "position"
            )
        )
        # Unpublished tracks stay out of the public listing but the count should
        # still reflect reality, so drafts are only hidden, not dropped silently.
        visible = [entry.track for entry in entries if entry.track.status == "published"]
        hidden = len(entries) - len(visible)

        is_collaborator = bool(
            user.is_authenticated
            and (
                playlist.owner_id == user.pk
                or user.is_superuser
                or playlist.collaborators.filter(pk=user.pk).exists()
            )
        )

        total_seconds = sum(t.duration_seconds for t in visible)

        context.update(
            {
                "tracks": visible,
                "entries": [entry for entry in entries if entry.track.status == "published"],
                "hidden_count": hidden if not is_collaborator else 0,
                "can_edit": is_collaborator,
                "is_following": user.is_authenticated
                and playlist.followers.filter(pk=user.pk).exists(),
                "follower_count": playlist.followers.count(),
                "total_seconds": total_seconds,
                "duration_display": human_duration(total_seconds),
            }
        )
        return context


class PlaylistListView(ListView):
    """Public playlist directory, with the viewer's own playlists alongside."""

    template_name = "playlists/list.html"
    context_object_name = "playlists"
    paginate_by = 24

    def get_queryset(self):
        return visible_playlists(self.request.user, public_only=True).order_by(
            "-total_plays", "-created_at"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["mine"] = (
            visible_playlists(user).filter(owner=user).order_by("-created_at")
            if user.is_authenticated
            else Playlist.objects.none()
        )
        return context
