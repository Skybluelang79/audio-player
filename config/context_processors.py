from django.conf import settings
from django.db.models import Count


def site_context(request):
    """Expose brand, media info and the sidebar library to every template.

    The sidebar shows the signed-in listener's own playlists. It is built here
    rather than inside a view so it is present on every page without each view
    having to remember it. The queryset is annotated with a track count so the
    sidebar never triggers a query per playlist.
    """
    playlists = []
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        # Imported lazily: the context processor loads during app startup, and
        # importing the model at module scope would run before apps are ready.
        from playlists.models import Playlist

        playlists = list(
            Playlist.objects.filter(owner=user)
            .annotate(sidebar_track_count=Count("tracks"))
            .order_by("-updated_at")[:40]
        )

    return {
        "SITE_NAME": "AURALIS",
        "SITE_TAGLINE": "Audio distribution core",
        "MEDIA_URL": settings.MEDIA_URL,
        "API_ROOT": "/api/",
        "sidebar_playlists": playlists,
    }
