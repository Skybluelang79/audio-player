"""django-filter definitions backing the search/filter API."""

import django_filters
from django.db.models import Count, Q

from .models import Album, Artist, Genre, Track


class GenreFilter(django_filters.FilterSet):
    # Partial match, consistent with ArtistFilter.name.
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")

    class Meta:
        model = Genre
        fields = ["name", "slug"]


class ArtistFilter(django_filters.FilterSet):
    name = django_filters.CharFilter(field_name="name", lookup_expr="icontains")
    genre = django_filters.ModelMultipleChoiceFilter(
        field_name="genres", queryset=Genre.objects.all(), conjoined=False
    )
    country = django_filters.CharFilter(lookup_expr="iexact")
    verified = django_filters.BooleanFilter()
    min_listeners = django_filters.NumberFilter(field_name="monthly_listeners", lookup_expr="gte")
    has_tracks = django_filters.BooleanFilter(method="filter_has_tracks")

    class Meta:
        model = Artist
        fields = ["verified", "country"]

    def filter_has_tracks(self, queryset, name, value):
        if value is None:
            return queryset
        return queryset.filter(tracks__isnull=not value).distinct()


class AlbumFilter(django_filters.FilterSet):
    title = django_filters.CharFilter(lookup_expr="icontains")
    artist = django_filters.ModelMultipleChoiceFilter(
        field_name="artist", queryset=Artist.objects.all()
    )
    genre = django_filters.ModelMultipleChoiceFilter(
        field_name="genres", queryset=Genre.objects.all()
    )
    album_type = django_filters.MultipleChoiceFilter(choices=Album.AlbumType.choices)
    released_after = django_filters.DateFilter(field_name="release_date", lookup_expr="gte")
    released_before = django_filters.DateFilter(field_name="release_date", lookup_expr="lte")
    release_year = django_filters.NumberFilter(field_name="release_date__year")

    class Meta:
        model = Album
        fields = ["album_type"]


class TrackFilter(django_filters.FilterSet):
    title = django_filters.CharFilter(lookup_expr="icontains")
    artist = django_filters.ModelMultipleChoiceFilter(
        field_name="artist", queryset=Artist.objects.all()
    )
    album = django_filters.ModelMultipleChoiceFilter(
        field_name="album", queryset=Album.objects.all()
    )
    genre = django_filters.ModelMultipleChoiceFilter(
        field_name="genres", queryset=Genre.objects.all()
    )
    status = django_filters.MultipleChoiceFilter(choices=Track.Status.choices)
    explicit = django_filters.BooleanFilter()
    min_duration = django_filters.NumberFilter(field_name="duration_seconds", lookup_expr="gte")
    max_duration = django_filters.NumberFilter(field_name="duration_seconds", lookup_expr="lte")
    min_plays = django_filters.NumberFilter(field_name="play_count", lookup_expr="gte")
    released_after = django_filters.DateFilter(field_name="release_date", lookup_expr="gte")
    released_before = django_filters.DateFilter(field_name="release_date", lookup_expr="lte")
    release_year = django_filters.NumberFilter(field_name="release_date__year")
    uploaded_after = django_filters.DateTimeFilter(field_name="created_at", lookup_expr="gte")
    isrc = django_filters.CharFilter(lookup_expr="iexact")
    trending = django_filters.BooleanFilter(method="filter_trending")

    class Meta:
        model = Track
        fields = ["status", "explicit"]

    def filter_trending(self, queryset, name, value):
        if value is None:
            return queryset
        return queryset.trending() if value else queryset.order_by("play_count")


class TrackSearchFilter(django_filters.FilterSet):
    """Free-text search across title, artist and album."""

    q = django_filters.CharFilter(method="filter_q")
    genre = django_filters.CharFilter(field_name="genres__slug", lookup_expr="iexact")

    class Meta:
        model = Track
        fields = []

    def filter_q(self, queryset, name, value):
        value = value.strip()
        if not value:
            return queryset
        return queryset.filter(
            Q(title__icontains=value)
            | Q(artist__name__icontains=value)
            | Q(album__title__icontains=value)
            | Q(isrc__iexact=value)
        ).distinct()


def annotate_counts(queryset, model_name):
    """Attach the counters the list serializers expect.

    Aliases are prefixed with ``agg_`` because ``Artist.track_count``,
    ``Artist.album_count`` and ``Album.track_count`` are ``property`` objects.
    Annotating a name that a property already owns is worse than useless: the
    data descriptor wins on attribute access so the annotation is ignored, and
    ``QuerySet.get()``/``Model.from_db`` raises
    ``AttributeError: property 'track_count' ... has no setter``. The
    serializers read ``agg_*`` first and fall back to the properties.
    """
    if model_name == "track":
        return queryset.annotate(favorite_count=Count("favorited_by", distinct=True))
    if model_name == "artist":
        return queryset.annotate(
            agg_track_count=Count("tracks", distinct=True),
            agg_album_count=Count("albums", distinct=True),
        )
    if model_name == "album":
        return queryset.annotate(agg_track_count=Count("tracks", distinct=True))
    if model_name == "genre":
        return queryset.annotate(agg_track_count=Count("tracks", distinct=True))
    return queryset
