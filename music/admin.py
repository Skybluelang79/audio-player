"""Genre, Artist, Album and Track admin with useful inlines and list filters."""

from django.contrib import admin
from django.db.models import Count
from django.utils.html import format_html

from .filters import annotate_counts
from .models import Album, Artist, Genre, Track, TrackComment, TrackFeaturedBy


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "color_swatch", "track_total")
    list_display_links = ("name",)
    search_fields = ("name", "description", "slug")
    prepopulated_fields = {"slug": ("name",)}
    fields = ("name", "slug", "color", "description")

    @admin.display(description="Colour")
    def color_swatch(self, obj):
        return format_html(
            '<span style="display:inline-block;width:44px;height:16px;'
            'border-radius:3px;background:{};border:1px solid #555"></span> {}',
            obj.color,
            obj.color,
        )

    @admin.display(description="Tracks")
    def track_total(self, obj):
        return obj.tracks.count()

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("tracks")


class TrackInline(admin.TabularInline):
    model = Track
    extra = 0
    fields = ("track_number", "title", "status", "duration_display", "play_count", "cover_thumb")
    readonly_fields = ("duration_display", "play_count", "cover_thumb")
    ordering = ("track_number", "title")
    show_change_link = True

    @admin.display(description="Length")
    def duration_display(self, obj):
        return obj.duration_display

    @admin.display(description="Cover")
    def cover_thumb(self, obj):
        if not obj.cover_image:
            return "—"
        return format_html(
            '<img src="{}" style="height:40px;border-radius:4px;object-fit:cover" />',
            obj.cover_image.url,
        )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("artist")


class FeaturedPlacementInline(admin.TabularInline):
    model = TrackFeaturedBy
    extra = 0
    raw_id_fields = ("track", "curator")


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    list_display = (
        "name", "verified_badge", "country", "track_count", "album_count",
        "monthly_listeners", "image_thumb", "created_at",
    )
    list_display_links = ("name",)
    list_filter = ("verified", "country", "genres", "created_at")
    search_fields = ("name", "bio", "slug", "website")
    prepopulated_fields = {"slug": ("name",)}
    filter_horizontal = ("genres", "followers")
    autocomplete_fields = ("owner",)
    readonly_fields = ("created_at", "updated_at", "monthly_listeners", "image_thumb")
    list_per_page = 25
    inlines = [TrackInline]
    fieldsets = (
        ("Identity", {"fields": ("name", "slug", "image_thumb", "bio", "verified", "owner")}),
        ("Presence", {"fields": ("genres", "country", "website", "followers")}),
        ("Metrics", {"fields": ("monthly_listeners",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
    actions = ("mark_verified", "recalculate_listeners", "sync_snapshots")

    @admin.display(boolean=True, description="Verified", ordering="verified")
    def verified_badge(self, obj):
        return obj.verified

    @admin.display(description="Tracks", ordering="track_count")
    def track_count(self, obj):
        return obj.track_count

    @admin.display(description="Albums", ordering="album_count")
    def album_count(self, obj):
        return obj.album_count

    @admin.display(description="Image")
    def image_thumb(self, obj):
        if not obj.image:
            return "—"
        return format_html(
            '<img src="{}" style="height:56px;border-radius:6px;object-fit:cover" />', obj.image.url
        )

    def get_queryset(self, request):
        # ``agg_*`` aliases: ``Artist.track_count``/``album_count`` are properties,
        # and a same-named annotation breaks model instantiation from rows.
        return annotate_counts(super().get_queryset(request), "artist")

    @admin.action(description="Mark selected artists as verified")
    def mark_verified(self, request, queryset):
        updated = queryset.update(verified=True)
        self.message_user(request, f"{updated} artist(s) verified.", extra_tags="success")

    @admin.action(description="Recalculate monthly listeners")
    def recalculate_listeners(self, request, queryset):
        total = 0
        for artist in queryset:
            total += artist.recalculate_monthly_listeners()
        self.message_user(request, f"Refreshed {queryset.count()} artist(s), {total} listeners counted.")

    @admin.action(description="Write monthly listener snapshots")
    def sync_snapshots(self, request, queryset):
        from datetime import date

        from analytics.models import ArtistListenerSnapshot

        month = date.today().replace(day=1)
        created = 0
        for artist in queryset:
            ArtistListenerSnapshot.objects.update_or_create(
                artist=artist,
                month=month,
                defaults={"monthly_listeners": artist.monthly_listeners, "total_plays": artist.total_plays},
            )
            created += 1
        self.message_user(request, f"{created} snapshot(s) written for {month:%Y-%m}.")


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    list_display = (
        "title", "artist", "album_type", "release_date", "track_count",
        "total_plays", "cover_thumb", "created_at",
    )
    list_display_links = ("title",)
    list_filter = ("album_type", "release_date", "label", "genres", "created_at")
    search_fields = ("title", "artist__name", "label", "catalog_number", "slug")
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ("artist",)
    filter_horizontal = ("genres",)
    readonly_fields = ("created_at", "updated_at", "cover_thumb")
    date_hierarchy = "release_date"
    list_select_related = ("artist",)
    inlines = [TrackInline]
    fieldsets = (
        ("Album", {"fields": ("title", "slug", "artist", "album_type", "release_date")}),
        ("Artwork & metadata", {"fields": ("cover_thumb", "cover_image", "label", "catalog_number", "description")}),
        ("Classification", {"fields": ("genres",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
    actions = ("mark_as_single",)

    @admin.display(description="Tracks")
    def track_count(self, obj):
        return obj.track_count

    @admin.display(description="Plays", ordering="total_plays")
    def total_plays(self, obj):
        return obj.total_plays

    @admin.display(description="Cover")
    def cover_thumb(self, obj):
        if not obj.cover_image:
            return "—"
        return format_html(
            '<img src="{}" style="height:56px;border-radius:6px;object-fit:cover" />', obj.cover_image.url
        )

    @admin.action(description="Mark selected albums as Singles")
    def mark_as_single(self, request, queryset):
        updated = queryset.update(album_type=Album.AlbumType.SINGLE)
        self.message_user(request, f"{updated} album(s) marked as singles.", extra_tags="success")


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = (
        "title", "artist", "album", "status_badge", "duration_display",
        "play_count", "like_count", "favorites", "cover_thumb", "created_at",
    )
    list_display_links = ("title",)
    list_filter = (
        "status", "explicit", "release_date", "genres", "created_at", "album__album_type",
    )
    search_fields = ("title", "slug", "artist__name", "album__title", "isrc")
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ("artist",)
    filter_horizontal = ("genres",)
    readonly_fields = (
        "play_count", "like_count", "download_count", "file_size_bytes",
        "created_at", "updated_at", "audio_player", "cover_thumb", "file_size_display",
    )
    list_per_page = 30
    list_select_related = ("artist", "album")
    date_hierarchy = "created_at"
    inlines = [FeaturedPlacementInline]
    fieldsets = (
        (
            "Track",
            {
                "fields": (
                    "title", "slug", "artist", "album", "track_number",
                    "genres", "explicit", "status",
                )
            },
        ),
        (
            "Media",
            {
                "fields": (
                    "audio_file", "audio_player", "cover_thumb", "cover_image",
                    "duration_seconds", "bitrate_kbps", "sample_rate_hz",
                    "file_size_display", "file_size_bytes",
                )
            },
        ),
        ("Release", {"fields": ("release_date", "isrc", "lyrics")}),
        (
            "Metrics",
            {"fields": ("play_count", "like_count", "download_count", "plays_today_display")},
        ),
        (
            "Timestamps",
            {"fields": ("created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )
    actions = ("publish_tracks", "archive_tracks", "reset_play_counts")

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        colours = {
            Track.Status.PUBLISHED: "#00E5A0",
            Track.Status.DRAFT: "#FFB020",
            Track.Status.PROCESSING: "#4DA3FF",
            Track.Status.ARCHIVED: "#8A8FA3",
        }
        colour = colours.get(obj.status, "#8A8FA3")
        return format_html(
            '<span style="padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;'
            'color:{};background:rgba(255,255,255,.06);border:1px solid {}44">{}</span>',
            colour,
            colour,
            obj.get_status_display(),
        )

    @admin.display(description="Length", ordering="duration_seconds")
    def duration_display(self, obj):
        return obj.duration_display

    @admin.display(description="Plays", ordering="play_count")
    def play_count(self, obj):
        return f"{obj.play_count:,}"

    @admin.display(description="Likes", ordering="like_count")
    def like_count(self, obj):
        return f"{obj.like_count:,}"

    @admin.display(description="Favourites")
    def favorites(self, obj):
        return obj.favorited_by.count()

    @admin.display(description="Plays today")
    def plays_today_display(self, obj):
        return obj.plays_today

    @admin.display(description="Cover")
    def cover_thumb(self, obj):
        if not obj.cover_image:
            return "—"
        return format_html(
            '<img src="{}" style="height:64px;border-radius:6px;object-fit:cover" />', obj.cover_image.url
        )

    @admin.display(description="Preview")
    def audio_player(self, obj):
        if not obj.audio_file:
            return "—"
        return format_html('<audio controls preload="none" src="{}" style="width:320px"></audio>', obj.audio_file.url)

    @admin.display(description="Size")
    def file_size_display(self, obj):
        from .storage import human_size

        return human_size(obj.file_size_bytes)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("artist", "album")

    @admin.action(description="Publish selected tracks")
    def publish_tracks(self, request, queryset):
        updated = queryset.update(status=Track.Status.PUBLISHED)
        self.message_user(request, f"{updated} track(s) published.", extra_tags="success")

    @admin.action(description="Archive selected tracks")
    def archive_tracks(self, request, queryset):
        updated = queryset.update(status=Track.Status.ARCHIVED)
        self.message_user(request, f"{updated} track(s) archived.")

    @admin.action(description="Reset play counts to zero")
    def reset_play_counts(self, request, queryset):
        updated = queryset.update(play_count=0)
        self.message_user(request, f"Reset play counts on {updated} track(s).", extra_tags="warning")


@admin.register(TrackComment)
class TrackCommentAdmin(admin.ModelAdmin):
    """Moderation queue for listener comments.

    Flagged comments are listed first by default so the moderation backlog is
    the default landing view rather than something staff have to remember to
    filter for.
    """

    list_display = (
        "excerpt", "track", "author", "status", "is_flagged", "is_pinned", "created_at",
    )
    list_display_links = ("excerpt",)
    list_filter = ("status", "is_flagged", "is_pinned", "created_at")
    search_fields = ("body", "user__email", "track__title")
    autocomplete_fields = ("track", "user", "parent")
    date_hierarchy = "created_at"
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-is_flagged", "-created_at")
    actions = ("approve_comments", "hide_comments", "clear_flags")

    @admin.display(description="Comment")
    def excerpt(self, obj):
        text = " ".join(obj.body.split())
        return text[:80] + ("…" if len(text) > 80 else "")

    @admin.display(description="Author", ordering="user__email")
    def author(self, obj):
        return obj.user.display_name or obj.user.email

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user", "track")

    @admin.action(description="Approve selected comments")
    def approve_comments(self, request, queryset):
        updated = queryset.update(status=TrackComment.Status.VISIBLE, is_flagged=False)
        self.message_user(request, f"Approved {updated} comment(s).", extra_tags="success")

    @admin.action(description="Hide selected comments")
    def hide_comments(self, request, queryset):
        updated = queryset.update(status=TrackComment.Status.HIDDEN)
        self.message_user(request, f"Hid {updated} comment(s).")

    @admin.action(description="Clear moderation flags")
    def clear_flags(self, request, queryset):
        updated = queryset.update(is_flagged=False)
        self.message_user(request, f"Cleared flags on {updated} comment(s).")
