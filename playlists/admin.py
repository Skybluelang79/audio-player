"""Playlist, favourite and history admin."""

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    Favorite,
    PlaybackQueue,
    PlaybackQueueItem,
    PlayHistory,
    Playlist,
    PlaylistFollow,
    PlaylistTrack,
)


class PlaylistTrackInline(admin.TabularInline):
    model = PlaylistTrack
    extra = 0
    fields = ("position", "track", "added_by", "added_at")
    readonly_fields = ("added_at",)
    autocomplete_fields = ("track", "added_by")
    ordering = ("position",)
    show_change_link = True


@admin.register(Playlist)
class PlaylistAdmin(admin.ModelAdmin):
    list_display = (
        "name", "owner", "visibility_badge", "track_total", "duration_display",
        "total_plays", "cover_thumb", "created_at",
    )
    list_display_links = ("name",)
    list_filter = ("visibility", "is_favorite", "created_at")
    search_fields = ("name", "description", "slug", "owner__email")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("owner",)
    filter_horizontal = ("collaborators",)
    readonly_fields = ("created_at", "updated_at", "total_plays", "track_total", "cover_thumb")
    list_select_related = ("owner",)
    list_per_page = 30
    inlines = [PlaylistTrackInline]
    fieldsets = (
        ("Playlist", {"fields": ("name", "slug", "owner", "description", "cover_thumb", "cover_image")}),
        ("Access", {"fields": ("visibility", "is_favorite", "collaborators")}),
        ("Stats", {"fields": ("track_total", "total_plays")}),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
    actions = ("publish_playlists", "make_private")

    @admin.display(description="Visibility", ordering="visibility")
    def visibility_badge(self, obj):
        colours = {
            Playlist.Visibility.PUBLIC: "#00E5A0",
            Playlist.Visibility.UNLISTED: "#4DA3FF",
            Playlist.Visibility.PRIVATE: "#8A8FA3",
        }
        colour = colours.get(obj.visibility, "#8A8FA3")
        return format_html(
            '<span style="padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;'
            'color:{};background:{}1A;border:1px solid {}55">{}</span>',
            colour, colour, colour, obj.get_visibility_display(),
        )

    @admin.display(description="Tracks")
    def track_total(self, obj):
        return obj.track_count

    @admin.display(description="Length")
    def duration_display(self, obj):
        return obj.duration_display

    @admin.display(description="Plays", ordering="total_plays")
    def total_plays(self, obj):
        return f"{obj.total_plays:,}"

    @admin.display(description="Cover")
    def cover_thumb(self, obj):
        if not obj.cover_image:
            return "—"
        return format_html(
            '<img src="{}" style="height:52px;border-radius:6px;object-fit:cover" />', obj.cover_image.url
        )

    @admin.action(description="Make selected playlists public")
    def publish_playlists(self, request, queryset):
        updated = queryset.update(visibility=Playlist.Visibility.PUBLIC)
        self.message_user(request, f"{updated} playlist(s) published.", extra_tags="success")

    @admin.action(description="Make selected playlists private")
    def make_private(self, request, queryset):
        updated = queryset.update(visibility=Playlist.Visibility.PRIVATE)
        self.message_user(request, f"{updated} playlist(s) made private.")


@admin.register(PlaylistTrack)
class PlaylistTrackAdmin(admin.ModelAdmin):
    list_display = ("playlist", "track", "position", "added_by", "added_at")
    list_filter = ("playlist", "added_at")
    search_fields = ("playlist__name", "track__title", "track__artist__name")
    autocomplete_fields = ("playlist", "track", "added_by")
    list_select_related = ("playlist", "track")


@admin.register(Favorite)
class FavoriteAdmin(admin.ModelAdmin):
    list_display = ("user", "track", "artist", "created_at")
    list_filter = ("created_at",)
    search_fields = ("user__email", "track__title")
    autocomplete_fields = ("user", "track")
    list_select_related = ("user", "track", "track__artist")
    date_hierarchy = "created_at"

    @admin.display(description="Artist")
    def artist(self, obj):
        return obj.track.artist.name


@admin.register(PlayHistory)
class PlayHistoryAdmin(admin.ModelAdmin):
    list_display = ("user", "track", "artist", "play_count", "completed", "last_played_at")
    list_filter = ("completed", "last_played_at")
    search_fields = ("user__email", "track__title")
    autocomplete_fields = ("user", "track")
    list_select_related = ("user", "track", "track__artist")
    date_hierarchy = "last_played_at"
    readonly_fields = ("play_count",)

    @admin.display(description="Artist")
    def artist(self, obj):
        return obj.track.artist.name


@admin.register(PlaylistFollow)
class PlaylistFollowAdmin(admin.ModelAdmin):
    list_display = ("user", "playlist", "created_at")
    search_fields = ("user__email", "playlist__name")
    autocomplete_fields = ("user", "playlist")
    list_select_related = ("user", "playlist")


class PlaybackQueueItemInline(admin.TabularInline):
    model = PlaybackQueueItem
    extra = 0
    autocomplete_fields = ("track",)
    ordering = ("position",)
    fields = ("position", "track")


@admin.register(PlaybackQueue)
class PlaybackQueueAdmin(admin.ModelAdmin):
    """Read-mostly view of saved playback queues, for support and debugging."""

    list_display = ("user", "size", "current_position", "shuffle", "repeat", "updated_at")
    list_display_links = ("user",)
    list_filter = ("shuffle", "repeat")
    search_fields = ("user__email",)
    autocomplete_fields = ("user",)
    date_hierarchy = "updated_at"
    readonly_fields = ("user", "updated_at")
    inlines = (PlaybackQueueItemInline,)

    @admin.display(description="Tracks")
    def size(self, obj):
        return obj.items.count()

    def has_add_permission(self, request):
        # A queue is created by the API on the owner's first PUT, never by hand.
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PlaybackQueueItem)
class PlaybackQueueItemAdmin(admin.ModelAdmin):
    list_display = ("queue", "position", "track", "artist", "added_at")
    search_fields = ("queue__user__email", "track__title")
    autocomplete_fields = ("queue", "track")
    list_select_related = ("queue__user", "track")

    @admin.display(description="Artist")
    def artist(self, obj):
        return obj.track.artist.name
