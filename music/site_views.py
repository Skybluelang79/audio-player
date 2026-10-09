"""Listener-facing HTML views for the public site.

``music/views.py`` serves programmatic clients through the REST API; these
views render the pages the web player links to. Both read the same
published-only queryset, so a track can never be visible on the site while it
is hidden from ``/api/tracks/``.

Every list surface emits ``<tr data-track-* >`` / ``<article data-track-* >``
markup (see ``templates/music/includes/track_table.html``) which
``static/js/player.js`` scrapes to build its queue. That keeps playback
working without shipping a second JSON payload per page.
"""

from django.core.exceptions import ValidationError
from django.db.models import Count, F, Q
from django.http import JsonResponse
from django.views.generic import DetailView, ListView, TemplateView

from analytics.search import MIN_QUERY_LENGTH, track_query
from playlists.filters import with_playlist_counters
from playlists.models import Playlist, trending_playlists

from .filters import annotate_counts
from .models import Album, Artist, Genre, Track

#: Rows rendered per shelf on the home page and genre pages.
SHELF_SIZE = 12

#: How many "more like this" rows a track page offers.
RELATED_SIZE = 8


def visible_playlists(user, *, public_only=False):
    """Playlists ``user`` may browse.

    Mirrors the API rule in ``playlists.views.PlaylistViewSet.get_queryset``:
    public rows for everyone, plus the viewer's own and shared playlists.
    Staff see everything so the site doubles as a moderation surface.
    """
    queryset = with_playlist_counters(Playlist.objects.select_related("owner"))
    if public_only:
        return queryset.filter(visibility=Playlist.Visibility.PUBLIC)
    if user.is_authenticated and not user.is_admin_staff:
        return queryset.filter(
            Q(visibility=Playlist.Visibility.PUBLIC)
            | Q(owner=user)
            | Q(collaborators=user)
        )
    if not user.is_authenticated:
        return queryset.filter(visibility=Playlist.Visibility.PUBLIC)
    return queryset


class SiteViewMixin:
    """Shared context for the listener-facing pages.

    ``nav_active`` has to come from here rather than a ``{% with %}`` inside a
    content block: the nav is rendered by ``site_base.html``, outside that
    block's scope.
    """

    nav_active = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav_active"] = self.nav_active
        return context


class HomeView(SiteViewMixin, TemplateView):
    """Landing page: the shelves a listener lands on."""

    template_name = "music/home.html"
    nav_active = "home"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "hero": Track.objects.popular()[:1],
                "trending": Track.objects.trending()[:SHELF_SIZE],
                "popular": Track.objects.popular()[:SHELF_SIZE],
                "recent": (
                    Track.objects.recently_released(days=180)
                    .with_listeners()
                    .order_by("-release_date", "title")[:SHELF_SIZE]
                ),
                "artists": annotate_counts(Artist.objects.all(), "artist").order_by(
                    "-monthly_listeners", "name"
                )[:SHELF_SIZE],
                "albums": annotate_counts(
                    Album.objects.select_related("artist"), "album"
                ).order_by("-release_date", "title")[:8],
                "genres": annotate_counts(Genre.objects.all(), "genre").order_by(
                    "-agg_track_count", "name"
                )[:12],
                "playlists": visible_playlists(self.request.user, public_only=True).order_by(
                    "-total_plays"
                )[:8],
                "trending_playlists": list(trending_playlists(limit=6)),
            }
        )
        return context


class TrackDetailView(SiteViewMixin, DetailView):
    """A single track plus the queue context needed to keep playing after it."""

    model = Track
    template_name = "music/track_detail.html"
    context_object_name = "track"
    slug_field = "slug"

    def get_queryset(self):
        # Drafts and archived tracks 404 for the public; staff keep access so a
        # preview link works before a release goes live.
        queryset = Track.objects.with_listeners()
        if self.request.user.is_authenticated and self.request.user.is_admin_staff:
            return queryset
        return queryset.published()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        track = self.object
        context.update(
            {
                "related": self.related_tracks(track),
                "in_album": (
                    track.album.tracks.with_listeners().order_by("track_number", "title")
                    if track.album_id
                    else Track.objects.none()
                ),
                "is_favorite": self.request.user.is_authenticated
                and track.favorited_by.filter(user=self.request.user).exists(),
                "plays_today": track.plays_today,
            }
        )
        return context

    def related_tracks(self, track):
        """Same-genre tracks by other artists, topped up with the artist's own.

        A pure genre query returns nothing for sparsely tagged catalogue, so the
        artist's back catalogue fills the remaining slots rather than leaving the
        shelf half-rendered.
        """
        genre_ids = list(track.genres.values_list("id", flat=True))
        candidates = (
            Track.objects.published()
            .with_listeners()
            .filter(Q(genres__in=genre_ids) if genre_ids else Q(pk__in=[]))
            .exclude(pk=track.pk)
            .distinct()
            .order_by("-play_count")[:RELATED_SIZE]
        )
        related = list(candidates)
        if len(related) < RELATED_SIZE:
            filler = (
                Track.objects.published()
                .with_listeners()
                .filter(artist=track.artist)
                .exclude(pk__in=[track.pk, *(t.pk for t in related)])
                .order_by("-play_count")
            )
            related.extend(filler[: RELATED_SIZE - len(related)])
        return related


class ArtistDetailView(SiteViewMixin, DetailView):
    model = Artist
    template_name = "music/artist_detail.html"
    context_object_name = "artist"
    slug_field = "slug"

    def get_queryset(self):
        return Artist.objects.prefetch_related("genres")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        artist = self.object
        tracks = artist.tracks.published().with_listeners()
        context.update(
            {
                "top_tracks": tracks.order_by("-play_count")[:10],
                "tracks": tracks.order_by("-release_date", "title")[:40],
                "albums": annotate_counts(artist.albums, "album").order_by(
                    "-release_date", "title"
                ),
                "genres": artist.genres.all(),
                "total_plays": artist.total_plays,
                "is_following": self.request.user.is_authenticated
                and artist.followers.filter(pk=self.request.user.pk).exists(),
            }
        )
        return context


class AlbumDetailView(SiteViewMixin, DetailView):
    model = Album
    template_name = "music/album_detail.html"
    context_object_name = "album"
    slug_field = "slug"

    def get_queryset(self):
        return Album.objects.select_related("artist").prefetch_related("genres")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        album = self.object
        context["tracks"] = (
            album.tracks.published()
            .with_listeners()
            .order_by("track_number", "title")
        )
        return context


class GenreDetailView(SiteViewMixin, DetailView):
    model = Genre
    template_name = "music/genre_detail.html"
    context_object_name = "genre"
    slug_field = "slug"

    def get_queryset(self):
        return Genre.objects.all()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        genre = self.object
        context.update(
            {
                "tracks": genre.tracks.published().with_listeners().order_by("-play_count")[:60],
                "artists": annotate_counts(
                    genre.artists.all(), "artist"
                ).order_by("-monthly_listeners", "name")[:12],
                "albums": annotate_counts(
                    genre.albums.select_related("artist"), "album"
                ).order_by("-release_date", "title")[:12],
            }
        )
        return context


class SearchView(SiteViewMixin, TemplateView):
    """Server-rendered search.

    Mirrors ``analytics.search.GlobalSearchView`` group-for-group so the HTML
    page and the JSON endpoint never disagree about what a term matches.
    """

    template_name = "music/search.html"
    #: Ceiling per group; the API allows 50 but a page of 250 rows is unusable.
    group_limit = 20

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = (self.request.GET.get("q") or "").strip()
        context["query"] = query
        context["results"] = {}
        context["total"] = 0
        context["min_length"] = MIN_QUERY_LENGTH

        if len(query) < MIN_QUERY_LENGTH:
            context["too_short"] = True
            return context

        user = self.request.user
        limit = self.group_limit
        results = {
            "tracks": Track.objects.published()
            .with_listeners()
            .filter(track_query(query))
            .distinct()[:limit],
            "artists": annotate_counts(
                Artist.objects.filter(Q(name__icontains=query) | Q(bio__icontains=query)),
                "artist",
            ).distinct()[:limit],
            "albums": annotate_counts(
                Album.objects.filter(
                    Q(title__icontains=query) | Q(artist__name__icontains=query)
                ).select_related("artist"),
                "album",
            ).distinct()[:limit],
            "genres": annotate_counts(
                Genre.objects.filter(
                    Q(name__icontains=query) | Q(description__icontains=query)
                ),
                "genre",
            )[:limit],
            "playlists": visible_playlists(user).filter(
                Q(name__icontains=query) | Q(description__icontains=query)
            ).distinct()[:limit],
        }
        context["results"] = {key: list(value) for key, value in results.items()}
        context["total"] = sum(len(rows) for rows in context["results"].values())
        return context


class LibraryView(SiteViewMixin, ListView):
    """Everything the signed-in listener has collected."""

    template_name = "music/library.html"
    context_object_name = "favorites"
    paginate_by = 30

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Track.objects.none()
        return (
            Track.objects.published()
            .with_listeners()
            .filter(favorited_by__user=user)
            .order_by("-favorited_by__created_at")
            .distinct()
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        if not user.is_authenticated:
            return context
        context.update(
            {
                "playlists": visible_playlists(user).filter(owner=user),
                # Track objects, not PlayHistory rows: the shared table include
                # reads .artist / .cover_image off whatever it is handed.
                "history": [
                    entry.track
                    for entry in user.history.select_related(
                        "track__artist", "track__album"
                    )[:20]
                ],
                "following": user.followed_artists.all()[:20],
            }
        )
        return context


def _manifest_entry(request, track):
    return {
        "id": str(track.pk),
        "title": track.title,
        "artist": track.artist.name,
        "artist_slug": track.artist.slug,
        "album": track.album.title if track.album_id else "",
        "album_slug": track.album.slug if track.album_id else "",
        "track_slug": track.slug,
        "url": request.build_absolute_uri(track.audio_file.url) if track.audio_file else "",
        "cover": track.cover_image.url if track.cover_image else "",
        "duration_seconds": track.duration_seconds,
        # Lets the player render the heart in the right state on first play
        # instead of waiting for the user to click it.
        "is_favorite": bool(
            request.user.is_authenticated
            and track.favorited_by.filter(user=request.user).exists()
        ),
    }


def track_manifest(request):
    """JSON playback descriptor for one or more tracks.

    The player already has id/title/artist from the row markup; this adds the
    fields a ``<audio>`` element needs but that markup should not carry (the
    absolute media URL and authoritative duration). Returning an array lets a
    client prime a whole queue in one request.
    """

    raw = request.GET.get("ids", "")
    candidates = [part.strip() for part in raw.split(",") if part.strip()][:100]

    # Trust the model's own pk converter instead of assuming an int or a UUID:
    # a malformed id otherwise raises deep inside field.get_prep_value() and
    # turns a typo in the query string into a 500.
    converter = Track._meta.pk.to_python
    ids = []
    for value in candidates:
        try:
            ids.append(converter(value))
        except (ValidationError, ValueError, TypeError):
            continue

    tracks = list(Track.objects.published().with_listeners().filter(pk__in=ids))

    # Preserve the caller's ordering: a queue is positional, and the IN clause
    # gives no ordering guarantee.
    order = {value: index for index, value in enumerate(ids)}
    tracks.sort(key=lambda t: order.get(t.pk, len(order)))

    return JsonResponse(
        {"tracks": [_manifest_entry(request, t) for t in tracks if t.audio_file]}
    )


def collection_manifest(request):
    """Playback descriptor for a whole album or playlist.

    A cover-art card has no track rows to scrape, so "Play" on one used to fall
    back to the entire page queue and start something unrelated. Asking the
    server also means the queue comes back in authoritative order -- album
    ``track_number``, then title; playlist entry ``position`` -- which the client
    cannot derive from markup.
    """

    kind = request.GET.get("kind", "")
    slug = request.GET.get("slug", "")

    if kind == "album":
        tracks = (
            Track.objects.published()
            .with_listeners()
            .filter(album__slug=slug)
            # track_number is nullable; unnumbered tracks belong at the end of
            # the record, not the front.
            .order_by(F("track_number").asc(nulls_last=True), "title")
        )
    elif kind == "playlist":
        # Reuse the one privacy rule the rest of the site uses, or this endpoint
        # would leak a private playlist's audio URLs to anyone guessing a slug.
        from playlists.models import Playlist

        playlist = (
            Playlist.objects.filter(slug=slug)
            .prefetch_related("collaborators", "playlist_tracks__track")
            .first()
        )
        if playlist is None or not playlist.is_visible_to(request.user):
            return JsonResponse({"kind": kind, "slug": slug, "tracks": []})

        entries = [entry.track for entry in playlist.playlist_tracks.all()]
        entries = [t for t in entries if t.status == Track.Status.PUBLISHED]
        tracks = entries
    else:
        return JsonResponse({"kind": kind, "slug": slug, "tracks": []})

    return JsonResponse(
        {
            "kind": kind,
            "slug": slug,
            "tracks": [_manifest_entry(request, t) for t in tracks if t.audio_file],
        }
    )
