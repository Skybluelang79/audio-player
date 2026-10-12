"""Analytics admin: raw events, rollups and snapshots."""

from django.contrib import admin
from django.utils.html import format_html

from .models import (
    ArtistListenerSnapshot,
    ArtistPayout,
    DailyPlatformStat,
    DailyTrackStat,
    PlayEvent,
)


@admin.register(PlayEvent)
class PlayEventAdmin(admin.ModelAdmin):
    list_display = ("played_at", "track", "user", "source", "listened_seconds", "completed", "ip_address")
    list_filter = ("source", "completed", "played_at")
    search_fields = ("track__title", "track__artist__name", "user__email", "session_key")
    autocomplete_fields = ("track", "user")
    list_select_related = ("track", "user")
    date_hierarchy = "played_at"
    list_per_page = 50
    readonly_fields = [f.name for f in PlayEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(DailyTrackStat)
class DailyTrackStatAdmin(admin.ModelAdmin):
    list_display = ("date", "track", "artist", "plays", "unique_listeners", "total_listened_seconds")
    list_filter = ("date",)
    search_fields = ("track__title", "track__artist__name")
    autocomplete_fields = ("track",)
    list_select_related = ("track", "track__artist")
    date_hierarchy = "date"
    list_per_page = 50

    @admin.display(description="Artist", ordering="track__artist__name")
    def artist(self, obj):
        return obj.track.artist.name


@admin.register(DailyPlatformStat)
class DailyPlatformStatAdmin(admin.ModelAdmin):
    list_display = ("date", "total_plays", "unique_listeners", "unique_tracks", "source_breakdown")
    list_filter = ("date",)
    date_hierarchy = "date"
    readonly_fields = ("by_source",)

    @admin.display(description="Sources")
    def source_breakdown(self, obj):
        if not obj.by_source:
            return "—"
        return format_html(
            " · ".join(f"<code>{key}: {value}</code>" for key, value in sorted(obj.by_source.items()))
        )

    def has_add_permission(self, request):
        return False


@admin.register(ArtistListenerSnapshot)
class ArtistListenerSnapshotAdmin(admin.ModelAdmin):
    list_display = ("month", "artist", "monthly_listeners", "total_plays")
    list_filter = ("month",)
    search_fields = ("artist__name",)
    autocomplete_fields = ("artist",)
    list_select_related = ("artist",)
    date_hierarchy = "month"


@admin.register(ArtistPayout)
class ArtistPayoutAdmin(admin.ModelAdmin):
    list_display = ("period", "artist", "plays", "amount", "status", "method", "paid_at")
    list_filter = ("status", "method", "period_end")
    search_fields = ("artist__name", "reference")
    autocomplete_fields = ("artist",)
    list_select_related = ("artist",)
    date_hierarchy = "period_end"
    list_per_page = 50
    readonly_fields = ("plays", "rate_per_play", "amount", "created_at", "updated_at")

    @admin.display(description="Period", ordering="period_end")
    def period(self, obj):
        return f"{obj.period_start:%Y-%m-%d} → {obj.period_end:%Y-%m-%d}"
