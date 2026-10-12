"""Staff dashboard views.

Everything here requires an active staff account. The HTML pages render the
same numbers the JSON endpoint at ``/api/analytics/summary/`` exposes, so a
third-party client and the built-in UI never drift apart.
"""

from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import DetailView, FormView, ListView, TemplateView, View

from analytics import services
from analytics.models import ArtistPayout, DailyPlatformStat, DailyTrackStat, PlayEvent
from music.filters import annotate_counts
from music.forms import (
    ArtistClaimForm,
    ArtistProfileForm,
    QuickTrackUploadForm,
    StudioTrackUploadForm,
)
from music.models import Album, Artist, Genre, Track, TrackComment
from playlists.filters import with_playlist_counters
from playlists.models import Playlist

from . import services as catalogue_services
from .services import BulkActionError, COUNTER_CHOICES


class StaffOnlyMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Redirect anonymous users to login; 403 for authenticated non-staff."""

    def test_func(self) -> bool:
        return self.request.user.is_authenticated and self.request.user.is_admin_staff

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied("Staff access is required to view the dashboard.")


class DashboardBase(StaffOnlyMixin):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = getattr(self, "active_nav", "overview")
        context["is_superuser"] = self.request.user.is_superuser
        return context


class DashboardHome(DashboardBase, TemplateView):
    template_name = "dashboard/home.html"
    active_nav = "overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        overview = services.catalogue_overview()
        growth = services.growth_series(days=30)
        engagement = services.engagement_stats(days=30)

        context.update(
            {
                "overview": overview,
                "growth": growth,
                "engagement": engagement,
                "top_tracks": services.top_tracks(limit=8),
                "trending_tracks": services.trending_tracks(limit=6),
                "play_distribution": services.play_distribution(limit=8),
                "genre_distribution": services.genre_distribution(limit=6),
                "timeseries": services.plays_timeseries(days=14),
                "uploads": services.new_content_series(days=14),
                "recent_tracks": Track.objects.select_related("artist", "album")[:8],
                "recent_playlists": (
                    with_playlist_counters(Playlist.objects.select_related("owner"))
                    .order_by("-created_at")[:5]
                ),
                "platform_stats": DailyPlatformStat.objects.first(),
            }
        )
        return context


class TrackAnalyticsView(DashboardBase, DetailView):
    template_name = "dashboard/tracks.html"
    active_nav = "tracks"
    model = Track
    context_object_name = "track"
    pk_url_kwarg = "pk"

    def get_queryset(self):
        return Track.objects.select_related("artist", "album").prefetch_related("genres")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        track = self.object
        last_30 = timezone.now() - timedelta(days=30)
        context.update(
            {
                "plays_today": track.plays_today,
                "favorites": track.favorited_by.count(),
                "appearances": track.playlist_entries.count(),
                "total_listened_seconds": PlayEvent.objects.filter(track=track).aggregate(
                    total=Sum("listened_seconds")
                )["total"] or 0,
                "recent_events": PlayEvent.objects.filter(track=track, played_at__gte=last_30)
                .select_related("user")[:15],
                "source_split": list(
                    PlayEvent.objects.filter(track=track)
                    .values("source")
                    .annotate(plays=Count("id"))
                    .order_by("-plays")
                ),
                "daily_stats": DailyTrackStat.objects.filter(track=track).order_by("date"),
            }
        )
        return context


class CatalogueView(DashboardBase, TemplateView):
    template_name = "dashboard/catalogue.html"
    active_nav = "catalogue"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "artists": annotate_counts(Artist.objects.all(), "artist").order_by(
                    "-monthly_listeners", "name"
                )[:20],
                "albums": annotate_counts(Album.objects.select_related("artist"), "album")[:20],
                "genres": annotate_counts(Genre.objects.all(), "genre").order_by(
                    "-agg_track_count"
                )[:20],
                "status_counts": {
                    row["status"]: row["count"]
                    for row in Track.objects.values("status").annotate(count=Count("id"))
                },
            }
        )
        return context


MODERATION_FILTERS = {
    "queue": Q(status=TrackComment.Status.PENDING) | Q(is_flagged=True),
    "pending": Q(status=TrackComment.Status.PENDING),
    "flagged": Q(is_flagged=True),
    "all": Q(),
}

# (value, label) pairs for the bulk-action buttons, in the order staff work
# through them. Kept beside the view that consumes them so the POST handler and
# the rendered buttons can never disagree on what a valid action is.
MODERATION_ACTIONS = (
    ("approve", "Approve"),
    ("hide", "Hide"),
    ("pending", "Send to review"),
    ("pin", "Pin"),
    ("unpin", "Unpin"),
    ("flag", "Flag"),
    ("unflag", "Clear flag"),
)


class ModerationQueueView(StaffOnlyMixin, ListView):
    """Comment moderation backlog.

    Default view is the actionable queue (pending review or flagged), which is
    what staff actually work through, rather than all comments newest-first.
    """

    model = TrackComment
    template_name = "dashboard/moderation.html"
    context_object_name = "comments"
    paginate_by = 25
    active_nav = "moderation"

    def get_queryset(self):
        queryset = (
            TrackComment.objects.select_related("user", "track", "track__artist")
            .prefetch_related("replies")
        )
        scope = self.request.GET.get("filter", "queue")
        if scope not in MODERATION_FILTERS:
            scope = "queue"
        queryset = queryset.filter(MODERATION_FILTERS[scope])

        search = self.request.GET.get("q", "").strip()
        if search:
            queryset = queryset.filter(
                Q(body__icontains=search)
                | Q(user__email__icontains=search)
                | Q(track__title__icontains=search)
            )

        ordering = self.request.GET.get("order", "")
        if ordering == "oldest":
            return queryset.order_by("created_at")
        if ordering == "track":
            return queryset.order_by("track__title", "created_at")
        return queryset.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        scope = request.GET.get("filter", "queue")
        if scope not in MODERATION_FILTERS:
            scope = "queue"

        context.update(
            {
                "active_nav": self.active_nav,
                "is_superuser": request.user.is_superuser,
                "scope": scope,
                "search": request.GET.get("q", ""),
                "ordering": request.GET.get("order", ""),
                "counts": self.status_counts(),
                "actions": MODERATION_ACTIONS,
                "page_range": self.page_range(context),
            }
        )
        return context

    def page_range(self, context):
        """1-based window of page numbers around the current page."""
        paginator = context["paginator"]
        number = context["page_obj"].number
        start = max(1, number - 2)
        end = min(paginator.num_pages, start + 4)
        return range(start, end + 1)

    @staticmethod
    def status_counts():
        """Headline numbers for the scope tabs.

        ``total`` counts every comment, while the others are deliberately
        computed with disjoint filters so the tabs add up.
        """
        rows = (
            TrackComment.objects.aggregate(
                total=Count("id"),
                pending=Count("id", filter=Q(status=TrackComment.Status.PENDING)),
                flagged=Count("id", filter=Q(is_flagged=True)),
                hidden=Count("id", filter=Q(status=TrackComment.Status.HIDDEN)),
            )
        )
        rows["visible"] = rows["total"] - rows["pending"] - rows["hidden"]
        rows["queue"] = (
            TrackComment.objects.filter(MODERATION_FILTERS["queue"]).count()
        )
        return rows


class ModerationActionView(StaffOnlyMixin, View):
    """Applies a bulk moderation decision to the selected comments.

    Separate from the list view so the queue stays a plain GET and the decision
    is an explicit POST.
    """

    ACTIONS = {
        "approve": {"status": TrackComment.Status.VISIBLE, "is_flagged": False},
        "hide": {"status": TrackComment.Status.HIDDEN, "is_flagged": False},
        "pending": {"status": TrackComment.Status.PENDING, "is_flagged": False},
        "pin": {"is_pinned": True},
        "unpin": {"is_pinned": False},
        "flag": {"is_flagged": True},
        "unflag": {"is_flagged": False},
    }

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "")
        if action not in self.ACTIONS:
            messages.error(request, "Unknown moderation action.")
            return redirect(self._return_url(request))

        ids = [value for value in request.POST.getlist("comment_ids") if value.isdigit()]
        if not ids:
            messages.warning(request, "Select at least one comment first.")
            return redirect(self._return_url(request))

        # default scope_filter: hide() strips is_flagged, so re-select by id
        # rather than by the scope the user was looking at.
        updated = TrackComment.objects.filter(pk__in=ids).update(**self.ACTIONS[action])

        verb = {
            "approve": "approved",
            "hide": "hidden",
            "pending": "moved to review",
            "pin": "pinned",
            "unpin": "unpinned",
            "flag": "flagged",
            "unflag": "unflagged",
        }[action]
        messages.success(request, f"{updated} comment(s) {verb}.")
        return redirect(self._return_url(request))

    @staticmethod
    def _return_url(request):
        """Return the user to the filtered queue they came from."""
        back = request.POST.get("next") or request.META.get("HTTP_REFERER")
        if back and back.startswith("/"):
            return back
        return reverse("dashboard:moderation")


class BulkCatalogueView(StaffOnlyMixin, ListView):
    """Filtered track list with bulk actions.

    The selection is expressed as filters rather than a saved query, so the
    export at ``/dashboard/catalogue/export.csv`` covers exactly the rows on
    screen.
    """

    model = Track
    template_name = "dashboard/bulk.html"
    context_object_name = "tracks"
    paginate_by = 50
    active_nav = "bulk"

    def get_filters(self):
        allowed = ("status", "artist_id", "album_id", "genre_id", "search", "has_audio")
        return {key: self.request.GET.get(key, "").strip() for key in allowed}

    def get_queryset(self):
        queryset = (
            Track.objects.select_related("artist", "album").prefetch_related("genres")
        )
        return catalogue_services.filter_tracks(queryset, **self.get_filters())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        filters = self.get_filters()
        query = urlencode({k: v for k, v in filters.items() if v})
        context.update(
            {
                "active_nav": self.active_nav,
                "is_superuser": self.request.user.is_superuser,
                "filters": filters,
                "artists": Artist.objects.order_by("name"),
                "albums": Album.objects.select_related("artist").order_by("artist__name", "title"),
                "genres": Genre.objects.order_by("name"),
                "statuses": Track.Status.choices,
                "matched_count": self.get_queryset().count(),
                "counters": COUNTER_CHOICES,
                # The export link must mirror the filter form, or a download
                # would silently cover a different set of rows.
                "export_query": f"?{query}" if query else "",
            }
        )
        return context


class BulkActionView(StaffOnlyMixin, View):
    """Applies one bulk catalogue decision to the current selection.

    Selected ids win; when none are ticked the action applies to everything the
    active filters match, which is what makes "publish everything still in
    draft" a single step.
    """

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action", "")
        handler = getattr(self, f"do_{action}", None)
        if handler is None:
            messages.error(request, "Unknown bulk action.")
            return redirect(self._return_url(request))

        ids = [value for value in request.POST.getlist("track_ids") if value.isdigit()]
        try:
            if ids:
                result = handler([int(value) for value in ids])
            else:
                result = handler(None)
        except BulkActionError as exc:
            messages.error(request, str(exc))
            return redirect(self._return_url(request))

        scope = f"{len(ids)} selected track(s)" if ids else f"{result.updated} matching track(s)"
        messages.success(request, f"{scope}: {result.summary}.")
        for note in result.notes:
            messages.warning(request, note)
        return redirect(self._return_url(request))

    # -- handlers. ``None`` means "every row matching the active filters". --

    def _targets(self, track_ids):
        if track_ids is not None:
            return track_ids
        request = self.request
        queryset = Track.objects.all()
        filters = {
            key: request.POST.get(key, "").strip()
            for key in ("status", "artist_id", "album_id", "genre_id", "search", "has_audio")
        }
        return list(
            catalogue_services.filter_tracks(queryset, **filters).values_list("pk", flat=True)
        )

    def do_publish(self, ids):
        return catalogue_services.set_status(self._targets(ids), Track.Status.PUBLISHED)

    def do_archive(self, ids):
        return catalogue_services.set_status(self._targets(ids), Track.Status.ARCHIVED)

    def do_draft(self, ids):
        return catalogue_services.set_status(self._targets(ids), Track.Status.DRAFT)

    def do_processing(self, ids):
        return catalogue_services.set_status(self._targets(ids), Track.Status.PROCESSING)

    def do_set_album(self, ids):
        raw = self.request.POST.get("album_id", "").strip()
        return catalogue_services.set_album(self._targets(ids), int(raw) if raw else None)

    def do_set_artist(self, ids):
        raw = self.request.POST.get("artist_id", "").strip()
        if not raw:
            raise BulkActionError("Choose an artist to move the tracks to.")
        return catalogue_services.set_artist(self._targets(ids), int(raw))

    def do_add_genres(self, ids):
        return catalogue_services.set_genres(
            self._targets(ids), self._genre_ids(), "add"
        )

    def do_replace_genres(self, ids):
        return catalogue_services.set_genres(
            self._targets(ids), self._genre_ids(), "replace"
        )

    def do_mark_explicit(self, ids):
        return catalogue_services.set_flag(self._targets(ids), "explicit", True)

    def do_clear_explicit(self, ids):
        return catalogue_services.set_flag(self._targets(ids), "explicit", False)

    def do_reset_counters(self, ids):
        return catalogue_services.reset_counters(self._targets(ids), self._counters())

    def do_delete(self, ids):
        if self.request.POST.get("confirm_delete") != "yes":
            raise BulkActionError("Tick the confirmation box to delete tracks.")
        return catalogue_services.delete_tracks(self._targets(ids))

    def _genre_ids(self):
        return [
            int(value)
            for value in self.request.POST.getlist("genre_ids")
            if value.isdigit()
        ]

    def _counters(self):
        allowed = {name for name, _ in COUNTER_CHOICES}
        return [
            name
            for name in self.request.POST.getlist("counters")
            if name in allowed
        ]

    @staticmethod
    def _return_url(request):
        back = request.POST.get("next") or request.META.get("HTTP_REFERER")
        if back and back.startswith("/"):
            return back
        return reverse("dashboard:bulk")


class CatalogueExportView(StaffOnlyMixin, View):
    """CSV download of the tracks matching the active catalogue filters."""

    #: Hard ceiling so a stray filter cannot stream the whole catalogue by accident.
    MAX_ROWS = 5000

    def get(self, request, *args, **kwargs):
        filters = {
            key: request.GET.get(key, "").strip()
            for key in ("status", "artist_id", "album_id", "genre_id", "search", "has_audio")
        }
        queryset = (
            catalogue_services.filter_tracks(
                Track.objects.select_related("artist", "album").prefetch_related("genres"),
                **filters,
            )
            .distinct()
            .order_by("artist__name", "album__title", "track_number", "title")
        )
        total = queryset.count()
        capped = queryset[: self.MAX_ROWS]
        body = catalogue_services.tracks_csv(capped)

        filename = catalogue_services.catalogue_export_filename("tracks")
        response = HttpResponse(body, content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        if total > self.MAX_ROWS:
            response["X-Export-Truncated"] = str(total)
        return response


class AudienceView(DashboardBase, TemplateView):
    template_name = "dashboard/audience.html"
    active_nav = "audience"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "growth_7": services.growth_series(days=7),
                "growth_30": services.growth_series(days=30),
                "engagement_7": services.engagement_stats(days=7),
                "engagement_30": services.engagement_stats(days=30),
                "timeseries": services.plays_timeseries(days=30),
                "hourly": services.listens_by_hour(days=30),
                "funnel": services.completion_funnel(days=30),
                "artist_leaderboard": services.leaderboard_artists(limit=10, days=30),
                "playlists": with_playlist_counters(
                    Playlist.objects.filter(visibility=Playlist.Visibility.PUBLIC)
                ).order_by("-total_plays")[:10],
                "platform_stats": DailyPlatformStat.objects.order_by("-date")[:14],
            }
        )
        return context


class QuickUploadView(StaffOnlyMixin, FormView):
    """Quick single-track upload straight from the dashboard."""

    template_name = "dashboard/upload.html"
    form_class = QuickTrackUploadForm
    success_url = reverse_lazy("dashboard:upload")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "upload"
        context["artists"] = Artist.objects.order_by("name")
        context["albums"] = Album.objects.select_related("artist").order_by("title")
        context["genres"] = Genre.objects.order_by("name")
        return context

    def form_valid(self, form):
        track = form.save()
        messages.success(
            self.request,
            f"“{track.title}” uploaded to {track.artist.name} and queued for processing.",
        )
        return redirect("dashboard:upload")

    def form_invalid(self, form):
        messages.error(self.request, "Upload failed. Review the highlighted fields.")
        return super().form_invalid(form)


class DashboardDataView(StaffOnlyMixin, TemplateView):
    """JSON mirror of the dashboard payload, for custom front-ends."""

    template_name = "dashboard/home.html"

    def get(self, request, *args, **kwargs):
        try:
            days = max(1, min(90, int(request.GET.get("days", 30))))
        except (TypeError, ValueError):
            days = 30
        return JsonResponse(
            {
                "overview": services.catalogue_overview(),
                "engagement": services.engagement_stats(days=min(days, 30)),
                "growth": services.growth_series(days=days),
                "timeseries": services.plays_timeseries(days=min(days, 30)),
                "hourly": services.listens_by_hour(days=min(days, 30)),
                "funnel": services.completion_funnel(days=min(days, 30)),
                "top_tracks": [
                    {
                        "id": track.id,
                        "title": track.title,
                        "artist": track.artist.name,
                        "play_count": track.play_count,
                        "status": track.status,
                    }
                    for track in services.top_tracks(limit=10, days=days)
                ],
                "play_distribution": services.play_distribution(),
                "genre_distribution": services.genre_distribution(),
            }
        )


# ---------------------------------------------------------------------------
# Artist studio — onboarding and self-service for artist accounts.
# ---------------------------------------------------------------------------


def _studio_artist(request, *, require_owned: bool = True):
    """The artist profile this request may manage.

    Staff may open any artist via ``?artist=``; artists get their owned
    profiles. Returns ``None`` when the user does not manage anyone, and raises
    ``PermissionDenied`` when they try to touch a profile they do not own.
    """
    user = request.user
    artist = None
    if user.is_admin_staff:
        pk = request.GET.get("artist") or request.POST.get("artist")
        if pk:
            artist = get_object_or_404(Artist, pk=pk)
    if artist is None:
        artist = Artist.objects.filter(owner=user).order_by("name").first()
    if artist is None:
        return None
    if require_owned and artist.owner_id != user.pk and not user.is_admin_staff:
        raise PermissionDenied("You can only manage your own artist profiles.")
    return artist


class StudioAccessMixin(LoginRequiredMixin):
    """Any signed-in account may reach the studio; page-level checks decide."""


class StudioHomeView(StudioAccessMixin, TemplateView):
    template_name = "dashboard/studio.html"
    active_nav = "studio"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = self.active_nav
        artist = _studio_artist(self.request, require_owned=False)
        context["artist"] = artist
        if artist is None:
            return context

        from decimal import Decimal

        payouts = ArtistPayout.objects.filter(artist=artist).aggregate(
            total=Sum("amount"),
            pending=Sum("amount", filter=Q(status=ArtistPayout.Status.PENDING)),
        )
        context.update(
            {
                "tracks": (
                    Track.objects.filter(artist=artist)
                    .select_related("album")
                    .annotate(comment_count=Count("comments__id", distinct=True))
                    .order_by("-created_at")[:50]
                ),
                "draft_count": Track.objects.filter(artist=artist, status=Track.Status.DRAFT).count(),
                "play_count": artist.total_plays,
                "follower_count": artist.followers.count(),
                "payouts_total": Decimal(payouts["total"] or 0),
                "payouts_pending": Decimal(payouts["pending"] or 0),
            }
        )
        return context


class StudioClaimView(StudioAccessMixin, FormView):
    """Onboarding: create a new artist profile or claim an unclaimed one."""

    template_name = "dashboard/studio_claim.html"
    form_class = ArtistClaimForm

    def get_success_url(self):
        return reverse_lazy("dashboard:studio")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = "studio"
        context["claimable"] = (Artist.objects.filter(owner__isnull=True).count())
        return context

    def form_valid(self, form):
        cleaned = form.cleaned_data
        if cleaned["mode"] == "create":
            artist = Artist.objects.create(
                name=cleaned["name"].strip(),
                country=cleaned.get("country", "") or "",
                website=cleaned.get("website", "") or "",
                bio=cleaned.get("bio", "") or "",
                owner=self.request.user,
            )
        else:
            artist = cleaned["existing_artist"]
            if artist.owner_id not in (None, self.request.user.pk):
                form.add_error(
                    None,
                    "That profile was claimed by someone else in the meantime. Refresh and pick again.",
                )
                return self.form_invalid(form)
            artist.owner = self.request.user
            artist.save(update_fields=["owner"])
        messages.success(
            self.request,
            f"You now manage “{artist.name}”. Finish your profile to complete onboarding.",
        )
        return super().form_valid(form)


class StudioProfileView(StudioAccessMixin, FormView):
    template_name = "dashboard/studio_profile.html"
    form_class = ArtistProfileForm
    active_nav = "studio"

    def get_success_url(self):
        return reverse_lazy("dashboard:studio")

    def get_artist(self):
        artist = _studio_artist(self.request, require_owned=True)
        if artist is None:
            raise PermissionDenied("Create or claim an artist profile first.")
        return artist

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_artist()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = self.active_nav
        context["artist"] = self.get_artist()
        return context

    def form_valid(self, form):
        artist = form.save()
        messages.success(self.request, f"“{artist.name}” profile updated.")
        return super().form_valid(form)


class StudioUploadView(StudioAccessMixin, FormView):
    template_name = "dashboard/studio_upload.html"
    form_class = StudioTrackUploadForm
    active_nav = "studio"

    def get_success_url(self):
        return reverse_lazy("dashboard:studio")

    def get_artist(self):
        artist = _studio_artist(self.request, require_owned=True)
        if artist is None:
            raise PermissionDenied("Create or claim an artist profile first.")
        return artist

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["artist"] = self.get_artist()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_nav"] = self.active_nav
        context["artist"] = self.get_artist()
        context["genres"] = Genre.objects.order_by("name")
        return context

    def form_valid(self, form):
        track = form.save()
        if not self.request.user.is_admin_staff and track.status == Track.Status.PUBLISHED:
            # A non-staff artist cannot self-publish: the upload lands as a
            # draft for staff review, matching the public API behaviour.
            track.status = Track.Status.DRAFT
            track.save(update_fields=["status"])
        messages.success(self.request, f"“{track.title}” uploaded and saved as a draft.")
        return super().form_valid(form)
