from django.conf import settings


def site_context(request):
    """Expose brand + media info to every template."""
    return {
        "SITE_NAME": "AURALIS",
        "SITE_TAGLINE": "Audio distribution core",
        "MEDIA_URL": settings.MEDIA_URL,
        "API_ROOT": "/api/",
    }
