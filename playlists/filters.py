import django_filters
from django.db.models import Count, Sum

from music.models import Track

from .models import Favorite, Playlist


class PlaylistFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(lookup_expr="icontains")
    owner = django_filters.UUIDFilter(field_name="owner_id")
    visibility = django_filters.MultipleChoiceFilter(choices=Playlist.Visibility.choices)
    min_tracks = django_filters.NumberFilter(method="filter_min_tracks")
    is_favorite = django_filters.BooleanFilter()
    created_after = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")

    class Meta:
        model = Playlist
        fields = ["is_favorite"]

    def filter_min_tracks(self, queryset, name, value):
        if value is None:
            return queryset
        return queryset.annotate(n=Count("tracks")).filter(n__gte=value)


class FavoriteFilter(django_filters.FilterSet):
    created_after = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    track = django_filters.ModelMultipleChoiceFilter(field_name="track", queryset=Track.objects.all())

    class Meta:
        model = Favorite
        fields = []


def with_playlist_counters(queryset):
    """Annotate aggregate counters for list responses.

    The annotation names deliberately differ from the ``Playlist.track_count``
    and ``Playlist.total_duration_seconds`` properties. Annotating a name that
    collides with a read-only ``@property`` makes Django try to *assign* it
    while instantiating the model from an annotated row (``queryset.get()``),
    which raises "property 'track_count' of 'Playlist' object has no setter".
    Keep these names disjoint from the model's properties.
    """
    return queryset.annotate(
        entry_count=Count("tracks", distinct=True),
        duration_total=Sum("tracks__duration_seconds"),
    )
