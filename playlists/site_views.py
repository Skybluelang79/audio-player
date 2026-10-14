"""Listener-facing HTML views for playlists.

Visibility is enforced through ``Playlist.is_visible_to`` rather than by
filtering a queryset, so a private playlist returns a real 404 to a stranger
and its slug cannot be probed.

The editor (create/rename/delete plus track management) lives here too. It is
the HTML twin of ``playlists.views.PlaylistViewSet`` and reuses the same
owner/collaborator rule for edits and owner-only rule for deletion.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Max
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)

from analytics.search import MIN_QUERY_LENGTH, track_query
from music.models import Track
from music.site_views import visible_playlists

from .forms import PlaylistForm
from .models import Playlist, PlaylistTrack


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


class PlaylistEditorMixin(LoginRequiredMixin):
    """Resolve a playlist by slug, then gate on owner/collaborator rights.

    A signed-in viewer who may not edit gets a 404 rather than a 403, so the
    edit surface stays as unguessable as the playlist itself (the same rule the
    detail page applies to private and unlisted rows).
    """

    def get_playlist(self):
        if hasattr(self, "_playlist"):
            return self._playlist
        playlist = get_object_or_404(
            Playlist.objects.select_related("owner").prefetch_related("collaborators"),
            slug=self.kwargs["slug"],
        )
        user = self.request.user
        is_editor = (
            playlist.owner_id == user.pk
            or user.is_superuser
            or playlist.collaborators.filter(pk=user.pk).exists()
        )
        if not is_editor:
            raise Http404("No playlist matches the given query.")
        self._playlist = playlist
        return playlist


class PlaylistCreateView(LoginRequiredMixin, CreateView):
    model = Playlist
    form_class = PlaylistForm
    template_name = "playlists/form.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["heading"] = "New playlist"
        return context

    def form_valid(self, form):
        # The form deliberately omits `owner`; it can only ever be the creator.
        form.instance.owner = self.request.user
        response = super().form_valid(form)
        messages.success(self.request, f"Created \u201c{self.object.name}\u201d.")
        return response


class PlaylistUpdateView(PlaylistEditorMixin, UpdateView):
    form_class = PlaylistForm
    template_name = "playlists/form.html"

    def get_object(self, queryset=None):
        return self.get_playlist()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["heading"] = f"Edit \u201c{self.object.name}\u201d"
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Saved \u201c{self.object.name}\u201d.")
        return response


class PlaylistDeleteView(PlaylistEditorMixin, DeleteView):
    template_name = "playlists/confirm_delete.html"

    def get_object(self, queryset=None):
        playlist = self.get_playlist()
        # Collaborators may edit but never destroy; that matches the API, where
        # `destroy` is owner-only even for a contributor.
        if playlist.owner_id != self.request.user.pk and not self.request.user.is_superuser:
            raise Http404("No playlist matches the given query.")
        return playlist

    def delete(self, request, *args, **kwargs):
        name = self.get_object().name
        messages.success(request, f"Deleted \u201c{name}\u201d.")
        return super().delete(request, *args, **kwargs)

    def get_success_url(self):
        return reverse("playlists:index")


class PlaylistManageView(PlaylistEditorMixin, TemplateView):
    """Add and remove tracks, the web twin of ``/add_track/`` and
    ``/tracks/<id>/``.

    POST is the write path (add/remove); GET renders the current entries plus
    an optional search box so a curator can find tracks to add.
    """

    template_name = "playlists/manage.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        playlist = self.get_playlist()
        query = (self.request.GET.get("q") or "").strip()
        existing_ids = set(
            playlist.playlist_tracks.values_list("track_id", flat=True)
        )

        results = []
        if len(query) >= MIN_QUERY_LENGTH:
            results = list(
                Track.objects.published()
                .with_listeners()
                .filter(track_query(query))
                .order_by("-play_count")
                .distinct()[:20]
            )

        context.update(
            {
                "playlist": playlist,
                "entries": list(
                    playlist.playlist_tracks.select_related("track__artist").order_by(
                        "position"
                    )
                ),
                "query": query,
                "min_length": MIN_QUERY_LENGTH,
                "results": results,
                "existing_ids": existing_ids,
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        playlist = self.get_playlist()
        action = request.POST.get("action")
        track_id = request.POST.get("track_id")

        if action == "add" and track_id:
            self._add_track(request, playlist, track_id)
        elif action == "remove" and track_id:
            self._remove_track(request, playlist, track_id)

        return HttpResponseRedirect(
            reverse("playlists:manage", kwargs={"slug": playlist.slug})
        )

    def _add_track(self, request, playlist, track_id):
        track = Track.objects.published().filter(pk=_as_int(track_id)).first()
        if track is None:
            messages.error(request, "That track is not available.")
            return
        next_position = (
            playlist.playlist_tracks.aggregate(top=Max("position"))["top"] or 0
        ) + 1
        _entry, created = PlaylistTrack.objects.get_or_create(
            playlist=playlist,
            track=track,
            defaults={"position": next_position, "added_by": request.user},
        )
        if created:
            messages.success(request, f"Added \u201c{track.title}\u201d.")
        else:
            messages.info(request, f"\u201c{track.title}\u201d is already in this playlist.")

    def _remove_track(self, request, playlist, track_id):
        deleted, _ = playlist.playlist_tracks.filter(
            track_id=_as_int(track_id)
        ).delete()
        if not deleted:
            return
        # Compact positions so the next add appends without a unique-clash.
        playlist.reorder(
            list(
                playlist.playlist_tracks.order_by("position").values_list(
                    "track_id", flat=True
                )
            )
        )
        messages.success(request, "Removed from playlist.")


def _as_int(value):
    """Coerce a posted track id to an int, ``None`` when it is not numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
