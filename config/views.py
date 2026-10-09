"""Infrastructure-level views that do not belong to any single app."""

from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@require_GET
@never_cache
def healthz(request):
    """Liveness/readiness probe for the platform.

    Render polls this to decide whether the service is healthy. It therefore
    touches the database: a process that cannot reach Postgres can serve HTML
    but cannot do anything useful, and we want that caught here rather than by
    a user's failed request.

    Deliberately unauthenticated and free of any version/host details, so it
    cannot be used to fingerprint the deployment.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        # 503 tells the platform the service is up but not yet ready, which is
        # more useful than a 500: it distinguishes "starting up / DB still
        # connecting" from a genuine crash.
        return HttpResponse(status=503)

    return JsonResponse({"status": "ok"})
